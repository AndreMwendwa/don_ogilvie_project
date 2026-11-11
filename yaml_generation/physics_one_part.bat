set part=5
python generate_physics_parts.py --parts %part% --preview-sections --model "deepseek/deepseek-v4.1-flash-1017"
python generate_physics_parts.py --parts %part% --inventory-only --model "deepseek/deepseek-v4.1-flash-1017"
python generate_physics_parts.py --parts %part% --model "deepseek/deepseek-v4.1-flash-1017"
python generate_physics_parts.py --validate-only --parts %part% --model "deepseek/deepseek-v4.1-flash-1017"
