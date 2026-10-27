from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import sys
from typing import Any

import yaml
from manim import *
from mutagen import File as MutagenFile


# ============================================================================
# OpenAI narration generation
# ============================================================================

class OpenAITTSService:
    """Generate narration clips directly through the OpenAI Speech API.

    The service deliberately does *not* add audio to Manim.  It writes one
    standalone audio file per narrated action.  The scene records when each
    clip should start, and the clips are mixed into the finished Manim video
    after rendering.
    """

    file_extension = ".mp3"

    def __init__(self, config: dict[str, Any]):
        self.config = config

    def cache_identity(self) -> dict[str, Any]:
        """Settings that should invalidate an existing narration clip."""
        return self.config

    def synthesize(self, text: str, output_path: Path) -> None:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError(
                "OpenAI narration requires the openai package. "
                "Run: pip install openai"
            ) from exc

        api_key_env = str(self.config.get("api_key_env", "OPENAI_API_KEY"))
        api_key = os.getenv(api_key_env)
        if not api_key:
            raise RuntimeError(
                f"OpenAI TTS requires an API key in environment variable {api_key_env!r}."
            )

        if len(text) > 4096:
            raise ValueError(
                "A single OpenAI narration clip cannot exceed 4096 characters. "
                "Split the narration across multiple YAML actions."
            )

        client = OpenAI(api_key=api_key)

        kwargs: dict[str, Any] = {
            # gpt-4o-mini-tts is supported by POST /audio/speech at the time this
            # file was generated. Keep the model configurable in YAML so it can
            # be changed without modifying the animation code.
            "model": self.config.get("model", "gpt-4o-mini-tts"),
            "voice": self.config.get("voice", "cedar"),
            "input": text,
            "response_format": "mp3",
            "speed": float(self.config.get("speed", 1.0)),
        }

        instructions = self.config.get("instructions")
        if instructions:
            kwargs["instructions"] = str(instructions)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with client.audio.speech.with_streaming_response.create(**kwargs) as response:
            response.stream_to_file(output_path)


# ============================================================================
# YAML-driven scene
# ============================================================================

class YAMLAnimation(Scene):
    """Render every question in one YAML file into one output video."""

    DEFAULT_FONT_SIZES = {
        "text": 30,
        "mathtex": 48,
        "tex": 40,
    }

    DIRECTIONS = {
        "UP": UP,
        "DOWN": DOWN,
        "LEFT": LEFT,
        "RIGHT": RIGHT,
        "UL": UL,
        "UR": UR,
        "DL": DL,
        "DR": DR,
        "ORIGIN": ORIGIN,
    }

    # ------------------------------------------------------------------
    # Input
    # ------------------------------------------------------------------
    def load_input(self) -> tuple[Path, dict[str, Any], dict[str, Any]]:
        yaml_path = self._yaml_path_from_cli()
        with yaml_path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        if not isinstance(data, dict):
            raise ValueError("The YAML root must be a mapping/dictionary.")

        video_settings = data.get("video", {})
        questions = data.get("questions")

        if not isinstance(video_settings, dict):
            raise ValueError("'video' must be a mapping/dictionary.")
        if not isinstance(questions, dict) or not questions:
            raise ValueError("YAML must contain a non-empty 'questions' mapping.")

        return yaml_path, video_settings, questions

    @staticmethod
    def _yaml_path_from_cli() -> Path:
        """Read YAML from an environment variable (preferred) or legacy CLI arg."""
        configured = os.environ.get("MANIM_YAML_FILE")
        if configured:
            path = Path(configured).expanduser().resolve()
            if not path.is_file():
                raise FileNotFoundError(f"YAML input file not found: {path}")
            return path

        # Backwards compatibility with commands passing -- questions.yaml.
        for arg in reversed(sys.argv):
            if arg.lower().endswith((".yaml", ".yml")):
                path = Path(arg).expanduser().resolve()
                if not path.is_file():
                    raise FileNotFoundError(f"YAML input file not found: {path}")
                return path
        raise ValueError(
            "Set MANIM_YAML_FILE before rendering, e.g. "
            "set MANIM_YAML_FILE=part_1_animations.yaml"
        )

    # ------------------------------------------------------------------
    # Voiceover configuration and OpenAI narration synthesis
    # ------------------------------------------------------------------
    @staticmethod
    def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
        result = deepcopy(base)
        for key, value in override.items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = YAMLAnimation.deep_merge(result[key], value)
            else:
                result[key] = deepcopy(value)
        return result

    def effective_voiceover_config(
        self,
        global_config: dict[str, Any],
        question_config: dict[str, Any] | None,
        action_spec: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        config = self.deep_merge(global_config, question_config or {})

        # Optional action-level override, useful for changing voice, speed, etc.
        if action_spec and isinstance(action_spec.get("voiceover_settings"), dict):
            config = self.deep_merge(config, action_spec["voiceover_settings"])

        return config

    @staticmethod
    def _safe_filename_component(value: str) -> str:
        value = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value)).strip("._")
        return value or "item"

    def _openai_config(self, voiceover_config: dict[str, Any]) -> dict[str, Any]:
        """Return OpenAI settings while retaining compatibility with old YAML.

        Both of these forms work:

            voiceover:
              provider: openai
              model: gpt-4o-mini-tts
              voice: cedar

        and the older nested form:

            voiceover:
              provider: openai
              openai:
                model: gpt-4o-mini-tts
                voice: cedar
        """
        provider = str(voiceover_config.get("provider", "openai")).lower()
        if provider == "none":
            return {"enabled": False}

        # Backward compatibility: older YAML files for the previous script
        # often contain ``provider: gtts``.  This OpenAI-only version should
        # still run those files without requiring every YAML to be edited.
        # Any legacy provider name is therefore treated as a request to use
        # OpenAI TTS.  Provider-specific legacy settings (for example the
        # ``gtts:`` block) are ignored.
        if provider != "openai":
            if not getattr(self, "_legacy_provider_warning_shown", False):
                print(
                    f"[OpenAI TTS] YAML specifies provider={provider!r}; "
                    "this OpenAI-only renderer will use OpenAI TTS instead. "
                    "For clarity, change provider to 'openai' in the YAML."
                )
                self._legacy_provider_warning_shown = True

        nested = voiceover_config.get("openai", {})
        if nested is None:
            nested = {}
        if not isinstance(nested, dict):
            raise ValueError("video.voiceover.openai must be a mapping/dictionary.")

        excluded = {
            "provider", "openai", "clips_dir", "cache_dir", "ffmpeg_binary",
            "replace_video", "keep_silent_video", "audio_bitrate", "write_manifest",
        }
        common = {k: v for k, v in voiceover_config.items() if k not in excluded}
        return {**common, **nested, "enabled": True}

    def _narration_clips_dir(self, voiceover_config: dict[str, Any]) -> Path:
        raw = voiceover_config.get(
            "clips_dir",
            voiceover_config.get("cache_dir", ".narration_clips"),
        )
        path = Path(str(raw)).expanduser()
        if not path.is_absolute():
            base = getattr(self, "_yaml_path", Path.cwd()).parent
            path = base / path
        path.mkdir(parents=True, exist_ok=True)
        return path.resolve()

    def make_voiceover(
        self,
        text: str,
        voiceover_config: dict[str, Any],
        *,
        question_number: str,
        action_index: int,
    ) -> tuple[str | None, float]:
        openai_config = self._openai_config(voiceover_config)
        if not openai_config.get("enabled", True):
            return None, 0.0

        service = OpenAITTSService(openai_config)
        clips_dir = self._narration_clips_dir(voiceover_config)

        digest_payload = {
            "service": "openai",
            "config": service.cache_identity(),
            "text": text,
        }
        digest = hashlib.sha1(
            json.dumps(digest_payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:10]

        q = self._safe_filename_component(question_number)
        audio_path = clips_dir / f"q_{q}_action_{action_index:03d}_{digest}.mp3"

        if not audio_path.exists():
            print(f"[OpenAI TTS] Generating {audio_path.name}")
            service.synthesize(text, audio_path)
        else:
            print(f"[OpenAI TTS] Reusing cached {audio_path.name}")

        audio = MutagenFile(str(audio_path))
        if audio is None or not getattr(audio, "info", None):
            raise RuntimeError(f"Could not determine duration of audio file: {audio_path}")

        duration = float(audio.info.length)
        return str(audio_path.resolve()), duration

    def _record_narration_event(
        self,
        *,
        text: str,
        audio_path: str,
        duration: float,
        start_time: float,
        question_number: str,
        action_index: int,
    ) -> None:
        self._narration_events.append(
            {
                "question": question_number,
                "action_index": action_index,
                "text": text,
                "audio_path": audio_path,
                "start_time": float(start_time),
                "duration": float(duration),
            }
        )

    # ------------------------------------------------------------------
    # Object construction and layout
    # ------------------------------------------------------------------
    def build_objects(self, specs: dict[str, dict[str, Any]]) -> dict[str, Mobject]:
        objects: dict[str, Mobject] = {}

        for object_id, spec in specs.items():
            objects[object_id] = self.make_object(object_id, spec)

        for object_id, spec in specs.items():
            self.apply_position(objects[object_id], spec.get("position", {}), objects)

        return objects

    def make_object(self, object_id: str, spec: dict[str, Any]) -> Mobject:
        kind = str(spec.get("type", "text")).lower()
        content = spec.get("content", "")
        font_size = spec.get("font_size", self.DEFAULT_FONT_SIZES.get(kind, 30))

        if kind == "text":
            obj = Text(str(content), font_size=font_size)
        elif kind == "mathtex":
            obj = MathTex(str(content), font_size=font_size)
        elif kind == "tex":
            obj = Tex(str(content), font_size=font_size)
        else:
            raise ValueError(
                f"Unsupported object type '{kind}' for '{object_id}'. "
                "Supported types: text, mathtex, tex."
            )

        if "scale" in spec:
            obj.scale(float(spec["scale"]))

        return obj

    def apply_position(
        self,
        obj: Mobject,
        position: dict[str, Any],
        objects: dict[str, Mobject],
    ) -> None:
        if not position:
            return

        if "to_edge" in position:
            obj.to_edge(
                self.direction(position["to_edge"]),
                buff=float(position.get("buff", DEFAULT_MOBJECT_TO_EDGE_BUFFER)),
            )

        if "next_to" in position:
            target = objects[position["next_to"]]
            obj.next_to(
                target,
                self.direction(position.get("direction", "DOWN")),
                buff=float(position.get("buff", DEFAULT_MOBJECT_TO_MOBJECT_BUFFER)),
            )

        if "move_to" in position:
            target = position["move_to"]
            if isinstance(target, str) and target in objects:
                obj.move_to(objects[target])
            elif isinstance(target, (list, tuple)) and len(target) in (2, 3):
                coords = list(target) + [0] * (3 - len(target))
                obj.move_to(coords)
            else:
                raise ValueError(f"Invalid move_to value: {target!r}")

        if "shift" in position:
            obj.shift(self.vector(position["shift"]))

    def direction(self, value: Any):
        if isinstance(value, str):
            key = value.upper()
            if key not in self.DIRECTIONS:
                raise ValueError(f"Unknown direction '{value}'.")
            return self.DIRECTIONS[key]
        return value

    @staticmethod
    def vector(value: Any):
        if not isinstance(value, (list, tuple)) or len(value) not in (2, 3):
            raise ValueError("shift must be a two- or three-number list.")
        coords = list(value) + [0] * (3 - len(value))
        return coords[0] * RIGHT + coords[1] * UP + coords[2] * OUT

    # ------------------------------------------------------------------
    # Generic actions
    # ------------------------------------------------------------------
    def animation_from_spec(
        self,
        spec: dict[str, Any],
        objects: dict[str, Mobject],
    ) -> Animation:
        action = str(spec["action"]).lower()

        if action == "write":
            return Write(objects[spec["target"]])
        if action == "create":
            return Create(objects[spec["target"]])
        if action == "fade_in":
            return FadeIn(objects[spec["target"]])
        if action == "fade_out":
            return FadeOut(objects[spec["target"]])
        if action == "transform":
            source = objects[spec["source"]]
            target = objects[spec["target"]]
            style = str(spec.get("style", "matching_tex")).lower()
            if style == "matching_tex":
                return TransformMatchingTex(source, target)
            if style == "replacement":
                return ReplacementTransform(source, target)
            if style == "transform":
                return Transform(source, target)
            raise ValueError(f"Unknown transform style '{style}'.")

        raise ValueError(f"Action '{action}' cannot be used inside a play group.")

    def run_action(
        self,
        action_spec: dict[str, Any],
        objects: dict[str, Mobject],
        voiceover_config: dict[str, Any],
        *,
        question_number: str,
        action_index: int,
    ) -> None:
        action = str(action_spec["action"]).lower()
        voiceover_text = action_spec.get("voiceover")

        audio_duration = 0.0
        if voiceover_text:
            effective_tts = self.effective_voiceover_config(
                voiceover_config,
                question_config=None,
                action_spec=action_spec,
            )
            narration_start = float(self.time)
            audio_path, audio_duration = self.make_voiceover(
                str(voiceover_text),
                effective_tts,
                question_number=question_number,
                action_index=action_index,
            )
            if audio_path:
                # Do NOT call self.add_sound().  Manim renders a video-only file.
                # The clip and its exact timeline position are saved for the
                # post-render FFmpeg muxing pass.
                self._record_narration_event(
                    text=str(voiceover_text),
                    audio_path=audio_path,
                    duration=audio_duration,
                    start_time=narration_start,
                    question_number=question_number,
                    action_index=action_index,
                )

        elapsed = 0.0

        if action == "wait":
            elapsed = float(action_spec.get("duration", 1.0))
            self.wait(elapsed)

        elif action == "narrate":
            elapsed = 0.0

        elif action == "highlight":
            target = objects[action_spec["target"]]
            shape = str(action_spec.get("shape", "rectangle")).lower()
            if shape != "rectangle":
                raise ValueError("Currently supported highlight shape: rectangle")

            highlight_id = action_spec.get("id")
            rectangle = SurroundingRectangle(
                target,
                buff=float(action_spec.get("buff", 0.2)),
            )
            if highlight_id:
                objects[highlight_id] = rectangle

            run_time = float(action_spec.get("run_time", 1.0))
            self.play(Create(rectangle), run_time=run_time)
            elapsed = run_time

        elif action == "play":
            children = action_spec.get("animations", [])
            if not children:
                raise ValueError("A play action requires a non-empty animations list.")
            animations = [self.animation_from_spec(child, objects) for child in children]
            run_time = float(action_spec.get("run_time", 1.0))
            self.play(*animations, run_time=run_time)
            elapsed = run_time

        elif action in {"write", "create", "fade_in", "fade_out", "transform"}:
            animation = self.animation_from_spec(action_spec, objects)
            run_time = float(action_spec.get("run_time", 1.0))
            self.play(animation, run_time=run_time)
            elapsed = run_time

        else:
            raise ValueError(
                f"Unsupported action '{action}'. Supported actions: write, create, "
                "fade_in, fade_out, transform, play, highlight, wait, narrate."
            )

        remaining_audio = audio_duration - elapsed
        if remaining_audio > 0:
            self.wait(remaining_audio)

        extra_wait = float(action_spec.get("after", 0.0))
        if extra_wait > 0:
            self.wait(extra_wait)

    # ------------------------------------------------------------------
    # Question/video orchestration
    # ------------------------------------------------------------------
    def clear_question(self, run_time: float = 0.5) -> None:
        visible = list(self.mobjects)
        if visible:
            self.play(*[FadeOut(mob) for mob in visible], run_time=run_time)
        self.clear()

    def render_question(
        self,
        question_number: str,
        content: dict[str, Any],
        global_voiceover: dict[str, Any],
    ) -> None:
        if not isinstance(content, dict):
            raise ValueError(f"Question {question_number!r} must be a mapping/dictionary.")

        object_specs = content.get("objects")
        actions = content.get("actions")

        if not isinstance(object_specs, dict) or not object_specs:
            raise ValueError(f"Question {question_number!r} needs a non-empty 'objects' mapping.")
        if not isinstance(actions, list) or not actions:
            raise ValueError(f"Question {question_number!r} needs a non-empty 'actions' list.")

        question_voiceover = content.get("voiceover", {})
        if question_voiceover is not None and not isinstance(question_voiceover, dict):
            raise ValueError(f"Question {question_number!r} 'voiceover' must be a mapping.")

        effective_voiceover = self.effective_voiceover_config(
            global_voiceover,
            question_voiceover or {},
        )

        objects = self.build_objects(object_specs)

        for action_index, action_spec in enumerate(actions, start=1):
            if not isinstance(action_spec, dict) or "action" not in action_spec:
                raise ValueError(
                    f"Every action in question {question_number!r} must be a mapping "
                    "with an 'action' key."
                )
            self.run_action(
                action_spec,
                objects,
                effective_voiceover,
                question_number=question_number,
                action_index=action_index,
            )

    def construct(self):
        yaml_path, video_settings, questions = self.load_input()
        self._yaml_path = yaml_path
        self._video_settings = video_settings
        self._narration_events: list[dict[str, Any]] = []

        global_voiceover = video_settings.get("voiceover", {})
        if not isinstance(global_voiceover, dict):
            raise ValueError("video.voiceover must be a mapping/dictionary.")

        clear_between = bool(video_settings.get("clear_between_questions", True))
        transition_run_time = float(video_settings.get("transition_run_time", 0.5))
        transition_wait = float(video_settings.get("transition_wait", 0.25))

        question_items = list(questions.items())
        for index, (question_number, content) in enumerate(question_items):
            self.render_question(str(question_number), content, global_voiceover)

            is_last = index == len(question_items) - 1
            if not is_last:
                if clear_between:
                    self.clear_question(run_time=transition_run_time)
                if transition_wait > 0:
                    self.wait(transition_wait)

    # ------------------------------------------------------------------
    # Post-render narration mixing / muxing
    # ------------------------------------------------------------------
    def _rendered_video_path(self) -> Path:
        writer = getattr(self.renderer, "file_writer", None)
        if writer is None:
            raise RuntimeError("Could not access Manim's file writer after rendering.")

        # Manim versions expose one or both of these names.
        for attr in ("final_file_path", "movie_file_path"):
            try:
                value = getattr(writer, attr)
            except (AttributeError, RuntimeError):
                continue
            if value:
                path = Path(value).expanduser().resolve()
                if path.exists():
                    return path

        raise RuntimeError(
            "Manim finished, but the rendered movie path could not be determined."
        )

    @staticmethod
    def _find_ffmpeg(config: dict[str, Any]) -> str:
        configured = config.get("ffmpeg_binary")
        if configured:
            candidate = Path(str(configured)).expanduser()
            if candidate.is_file():
                return str(candidate.resolve())
            found = shutil.which(str(configured))
            if found:
                return found
            raise RuntimeError(f"Configured FFmpeg executable was not found: {configured}")

        found = shutil.which("ffmpeg")
        if found:
            return found

        raise RuntimeError(
            "FFmpeg is required to combine the separately generated narration clips "
            "with the Manim video, but 'ffmpeg' was not found on PATH. Install FFmpeg "
            "or set video.voiceover.ffmpeg_binary in the YAML file."
        )

    def _write_narration_manifest(self, video_path: Path, config: dict[str, Any]) -> Path:
        clips_dir = self._narration_clips_dir(config)
        manifest_path = clips_dir / f"{video_path.stem}_narration_manifest.json"
        payload = {
            "source_yaml": str(getattr(self, "_yaml_path", "")),
            "rendered_video": str(video_path),
            "scene_duration": float(self.time),
            "clips": self._narration_events,
        }
        manifest_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return manifest_path

    @staticmethod
    def _mix_audio_events(
        ffmpeg: str,
        events: list[dict[str, Any]],
        output_path: Path,
        *,
        start_time: float,
        duration: float,
        source_video: Path | None = None,
        audio_codec: list[str] | None = None,
    ) -> None:
        """Mix a *small* collection of narration events without long CLI args.

        Events keep absolute Manim timestamps. Temporary layers are FLAC (lossless),
        so mixing batches does not repeatedly degrade the voiceover quality.
        """
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
        if source_video is not None:
            cmd.extend(["-i", str(source_video)])
        first_audio_input = 1 if source_video is not None else 0
        for event in events:
            cmd.extend(["-i", str(event["audio_path"])])

        filters = []
        labels = []
        for index, event in enumerate(events):
            label = f"n{index}"
            delay_ms = max(0, round((float(event["start_time"]) - start_time) * 1000))
            filters.append(
                f"[{first_audio_input + index}:a]adelay={delay_ms}:all=1[{label}]"
            )
            labels.append(f"[{label}]")

        if len(labels) == 1:
            final = labels[0]
        else:
            final = "[mixed]"
            filters.append(
                f"{''.join(labels)}amix=inputs={len(labels)}:"
                "normalize=0:dropout_transition=0[mixed]"
            )
        filters.append(f"{final}apad,atrim=duration={duration:.6f}[aout]")
        cmd.extend(["-filter_complex", ";".join(filters)])
        if source_video is not None:
            cmd.extend(["-map", "0:v:0", "-map", "[aout]", "-c:v", "copy"])
            cmd.extend(audio_codec or ["-c:a", "aac", "-b:a", "192k"])
            cmd.append("-shortest")
        else:
            cmd.extend(["-map", "[aout]", "-c:a", "flac"])
        cmd.append(str(output_path))

        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
        except (subprocess.CalledProcessError, OSError) as exc:
            detail = (getattr(exc, "stderr", "") or str(exc)).strip()
            raise RuntimeError(
                f"FFmpeg narration mixing failed: {detail[-2500:]}"
            ) from exc

    def _mux_narration_into_video(self) -> None:
        if not getattr(self, "_narration_events", None):
            print("[Narration] No narration clips were generated; skipping audio mux.")
            return

        voiceover_config = dict(getattr(self, "_video_settings", {}).get("voiceover", {}))
        provider = str(voiceover_config.get("provider", "openai")).lower()
        if provider == "none":
            return

        video_path = self._rendered_video_path()
        if video_path.suffix.lower() not in {".mp4", ".mov", ".m4v", ".webm"}:
            raise RuntimeError(
                f"Narration muxing expects a video output, but Manim produced {video_path.name!r}."
            )

        if bool(voiceover_config.get("write_manifest", True)):
            manifest_path = self._write_narration_manifest(video_path, voiceover_config)
            print(f"[Narration] Timeline manifest: {manifest_path}")

        ffmpeg = self._find_ffmpeg(voiceover_config)
        events = sorted(self._narration_events, key=lambda e: float(e["start_time"]))
        timeline_duration = max(
            float(self.time),
            max(float(e["start_time"]) + float(e["duration"]) for e in events),
        )
        temp_output = video_path.with_name(
            f"{video_path.stem}__narrated_tmp{video_path.suffix}"
        )
        if video_path.suffix.lower() == ".webm":
            codec = ["-c:a", "libopus", "-b:a", str(voiceover_config.get("audio_bitrate", "160k"))]
        else:
            codec = ["-c:a", "aac", "-b:a", str(voiceover_config.get("audio_bitrate", "192k"))]

        # Windows CreateProcess fails with WinError 206 if hundreds of input
        # filenames and an enormous filter_complex are placed on one command.
        # Collapse events into lossless FLAC batches, recursively if necessary.
        max_inputs = max(2, min(12, int(voiceover_config.get("mux_batch_size", 12))))
        print(f"[Narration] Mixing {len(events)} OpenAI narration clips into {video_path.name}")
        try:
            with tempfile.TemporaryDirectory(prefix="narration_mux_") as working_dir:
                work = Path(working_dir)
                layer = 0
                while len(events) > max_inputs:
                    collapsed = []
                    for batch_idx in range(0, len(events), max_inputs):
                        batch = events[batch_idx: batch_idx + max_inputs]
                        start = min(float(e["start_time"]) for e in batch)
                        end = max(float(e["start_time"]) + float(e["duration"]) for e in batch)
                        batch_path = work / f"layer_{layer:02d}_{batch_idx // max_inputs:04d}.flac"
                        self._mix_audio_events(
                            ffmpeg, batch, batch_path,
                            start_time=start, duration=max(0.001, end - start),
                        )
                        collapsed.append({
                            "audio_path": str(batch_path),
                            "start_time": start,
                            "duration": end - start,
                        })
                    print(f"[Narration] Mux layer {layer + 1}: {len(events)} -> {len(collapsed)} tracks")
                    events = collapsed
                    layer += 1

                self._mix_audio_events(
                    ffmpeg, events, temp_output,
                    start_time=0.0, duration=timeline_duration,
                    source_video=video_path, audio_codec=codec,
                )
        except Exception:
            if temp_output.exists():
                temp_output.unlink()
            raise

        keep_silent = bool(voiceover_config.get("keep_silent_video", False))
        replace_video = bool(voiceover_config.get("replace_video", True))

        if replace_video:
            if keep_silent:
                silent_copy = video_path.with_name(f"{video_path.stem}_silent{video_path.suffix}")
                shutil.copy2(video_path, silent_copy)
                print(f"[Narration] Silent video retained at: {silent_copy}")
            os.replace(temp_output, video_path)
            print(f"[Narration] Final narrated video: {video_path}")
        else:
            final_path = video_path.with_name(f"{video_path.stem}_narrated{video_path.suffix}")
            os.replace(temp_output, final_path)
            print(f"[Narration] Final narrated video: {final_path}")

    def render(self, *args, **kwargs):
        """Render Manim first, then mux separately generated narration clips."""
        result = super().render(*args, **kwargs)
        self._mux_narration_into_video()
        return result

