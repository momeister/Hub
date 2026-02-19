How to add ComfyUI workflows
==============================

1. Open ComfyUI web UI (http://localhost:8188)
2. Build or load your working workflow
3. Click "Save (API Format)" in ComfyUI
4. Save the JSON file to this directory with a descriptive name:
   - flux2.json
   - wan2.json
   - sdxl.json
   etc.

The filename (without .json) becomes the model name in Telegram.
For example, "flux2.json" maps to model name "flux2".

The skill will auto-discover all .json files in this directory.
It injects your prompt, dimensions, steps, and seed into the workflow
by finding standard ComfyUI node types:
  - CLIPTextEncode -> prompt text
  - EmptyLatentImage -> width, height, batch_size
  - KSampler / SamplerCustom -> steps, seed
  - SaveImage -> filename prefix
