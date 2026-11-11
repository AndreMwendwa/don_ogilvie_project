python chemistry_extract.py --parts 1
python generate_chemistry_parts.py --parts 1 --model "deepseek/deepseek-v4.1-flash-1017"
python generate_chemistry_parts.py --validate-only --parts 1 --model "deepseek/deepseek-v4.1-flash-1017"
python render_chemistry_videos.py --parts 1 --quality h --repair-latex --latex-model "deepseek/deepseek-v4.1-flash-1017"