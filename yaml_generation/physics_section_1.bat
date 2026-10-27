python generate_physics_parts.py --parts 1 --preview-sections --model "deepseek/deepseek-v4.1-flash-1017"
python generate_physics_parts.py --parts 1 --inventory-only --model "deepseek/deepseek-v4.1-flash-1017"
python generate_physics_parts.py --parts 1 --model "deepseek/deepseek-v4.1-flash-1017"
python generate_physics_parts.py --validate-only --parts 1 --model "deepseek/deepseek-v4.1-flash-1017"
python render_videos.py ^
  --parts 1 ^
  --input physics_output ^
  --renderer manim_multi_question_openai_mux_fixed.py ^
  --output physics_videos ^
  --quality l