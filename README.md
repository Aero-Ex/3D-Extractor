# 3D-Extractor

`3D-Extractor` is a Blender-based command-line tool for rendering clean multi-view texture outputs from 3D meshes.

It can generate:

- normal maps
- depth maps
- RGB renders
- albedo renders
- metallic maps
- roughness maps

Supported inputs: `.glb`, `.gltf`, `.obj`, `.stl`

---

## Requirements

- Blender (with Python API access)
- Python 3 (used by Blender; optional for orchestration use cases)

No extra Python packages are required.

---

## Quick start

### 1) Render one model

```bash
blender -b -P render.py -- \
  --input /path/to/model.glb \
  --output /path/to/output \
  --normals --depth --rgb --albedo
```

### 2) Batch render a folder

```bash
blender -b -P render.py -- \
  --input /path/to/models \
  --output /path/to/output \
  --parallel 4 \
  --normals --depth --rgb --albedo
```

---

## Output layout

For each model, outputs are grouped by pass and view:

```text
output/
  model_name/
    normals/
      front.png
      back.png
      left.png
      right.png
      top.png
      bottom.png
    depth/
      front.png
      ...
    rgb/
      ...
    albedo/
      ...
```

Views rendered per pass: `front`, `back`, `left`, `right`, `top`, `bottom`.

---

## CLI options

```text
--input       Path to a model file or directory of models (required)
--output      Output directory (required)
--resolution  Output image size, square (default: 2048)
--zoom        Camera zoom level; higher is tighter framing (default: 1.0)
--parallel    Number of parallel workers (default: 1)
--threads     Blender render threads per worker, 0 = auto (default: 0)
```

Texture-pass flags:

```text
--normals --depth --rgb --albedo --metallic --roughness
```

If no pass flags are provided, the script defaults to:

- `--normals`
- `--depth`
- `--rgb`
- `--albedo`

---

## Notes

- Single-file rendering should be run through Blender (`blender -b -P ...`).
- For large batches, increase `--parallel` gradually to match available CPU/GPU resources.
- Outputs are PNG RGBA at 16-bit depth.
