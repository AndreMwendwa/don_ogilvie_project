# Physics workbook generation (no Manim renderer modifications)

This is a separate physics-focused pipeline. The original mathematics generator and master prompt are retained.

## Section mapping

The PDF has seven QUESTION-ONLY workbook sets, mapped to numbered YAML parts. See `physics_sections.json` for 1-based PDF page ranges and boundaries. Parts 3 and 4 are two distinct SPH3U question sets, not a mistake. Every set has 25 numbered questions.

1. Set A — PDF pp. 8–10
2. Set B — pp. 12–15
3. First SPH3U set — pp. 20–22
4. Second SPH3U set — pp. 30–32
5. SPH4U numerical workbook — pp. 48–50
6. SPH4U mixed MCQ — pp. 63–69
7. First-year university MCQ — pp. 76–83

Extraction clips content BEFORE instructor solutions, including where questions and answers begin on the same physical page.

## Commands (Python 3.10+)

Install generation dependencies: `pip install -r requirements.txt`

Preview extracted question-only sections **offline**:

`python generate_physics_parts.py --parts 1-7 --preview-sections`

or `python physics_extract.py --parts 1-7`

Inspect files under `physics_output/section_previews` for symbol extraction errors.

Create question inventories using OpenRouter (costs API usage):

`python generate_physics_parts.py --parts 1-7 --inventory-only --model openai/gpt-4.1`

Generate YAML (after inspecting inventories):

`python generate_physics_parts.py --parts 1-7 --model openai/gpt-4.1`

The generator requires `OPENROUTER_API_KEY`. Model calls use the bundled `openrouter_client.py` (`OpenRouterClient`) with automatic JSON response-mode fallbacks (default model `openai/gpt-4.1`, configurable with `--model` or `OPENROUTER_MODEL`). Question text is extracted **locally** using PyMuPDF, then supplied to OpenRouter; the PDF itself is not uploaded. Models may use `json_schema`, `json_object`, or prompt-only JSON output when `--format-mode auto` (default); response JSON is validated against the requested schema. Image-only diagrams may require manual checking against the original PDF.

Validate existing generated YAMLs without API calls:

`python generate_physics_parts.py --parts 1-7 --validate-only`

Render using the **unchanged renderer** bundled here:

`python render_videos.py --parts 1-7 --input physics_output --renderer manim_multi_question_openai_mux_fixed.py --output physics_videos --quality l`

The renderer requires Manim, LaTeX, FFmpeg, `mutagen`, and `openai` (for TTS). Rendering needs a valid `OPENAI_API_KEY` if voiceover enabled. This particular renderer supports OpenAI speech, not OpenRouter TTS or gTTS.

**Important**: extraction and static YAML validation do not verify correctness or LaTeX rendering. Check inventories, do a short video test, then render more. This PDF has some multiple-choice keys/explanations that may not agree with calculations: independently verify instead of copying. PDF page indices are not printed workbook page numbers. No API runs or videos are included in this archive.

## API keys are separate

Windows CMD: `set OPENROUTER_API_KEY=YOUR_OPENROUTER_KEY` for extraction, YAML generation, and physics review. The **unchanged renderer** continues to require `OPENAI_API_KEY` when OpenAI narration is enabled. Keep both keys out of scripts, batch files committed to Git, and source control.

The original maths `generate_all_parts.py` remains unchanged and uses OpenAI; the physics-specific `generate_physics_parts.py` uses OpenRouter exclusively for model operations.

## Shared OpenRouterClient options

The physics generator imports `OpenRouterClient` from `openrouter_client.py` (the supplied transport module). It supports `--model`, `--format-mode auto|json_schema|json_object|prompt`, `--max-tokens`, `--timeout`, `--api-retries`, and `--pdf-engine`.

Example: `python generate_physics_parts.py --parts 1-2 --model openai/gpt-4.1 --format-mode auto --max-tokens 16000`

The transport uses `httpx` directly, supports retries and JSON parsing/validation fallbacks, and logs successful call usage to `physics_output/openrouter_usage.json`. PDF sections are still extracted locally (only question text is sent); `--pdf-engine` is reserved for future requests where PDF bytes are passed. The original maths pipeline and OpenAI TTS renderer are unchanged.
