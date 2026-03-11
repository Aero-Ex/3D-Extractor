import os
import argparse
import math
import sys
import time
import json
import tempfile
import atexit
from datetime import timedelta, datetime
import concurrent.futures
import subprocess
try:
    import bpy
    import mathutils
except ImportError:
    # Allow running as a master process to spawn blender workers
    pass

def get_ram_usage():
    """Returns (used_gb, total_gb, percent) of system RAM."""
    try:
        import psutil
        mem = psutil.virtual_memory()
        return mem.used / (1024**3), mem.total / (1024**3), mem.percent
    except ImportError:
        # Fallback for Linux without psutil
        try:
            with open('/proc/meminfo', 'r') as f:
                lines = f.readlines()
            info = {}
            for line in lines:
                parts = line.split(':')
                if len(parts) == 2:
                    info[parts[0].strip()] = int(parts[1].split()[0])
            total = info['MemTotal'] / (1024*1024)
            available = info['MemAvailable'] / (1024*1024)
            used = total - available
            return used, total, (used / total) * 100
        except:
            return 0, 0, 0

def is_render_complete(folder, expected_views):
    """Check if a folder contains all expected view files and they are non-empty."""
    if not os.path.exists(folder):
        return False
    for v in expected_views:
        p = os.path.join(folder, f"{v}.png")
        if not os.path.exists(p) or os.path.getsize(p) == 0:
            return False
    return True

def setup_common_settings(resolution=2048, threads=0):
    scene = bpy.context.scene
    scene.render.resolution_x = resolution
    scene.render.resolution_y = resolution
    scene.render.resolution_percentage = 100

    # Alpha Output settings
    scene.render.image_settings.file_format = 'OPEN_EXR'
    scene.render.image_settings.color_mode = 'RGBA'
    scene.render.image_settings.color_depth = '16'
    scene.render.image_settings.compression = 15 # Lower compression = much faster saving
    scene.render.film_transparent = True
    scene.render.dither_intensity = 0.0

    # Color Management: Standard is best for data
    scene.view_settings.view_transform = 'Standard'
    scene.view_settings.look = 'None'

    if threads > 0:
        scene.render.threads_mode = 'FIXED'
        scene.render.threads = threads
    else:
        scene.render.threads_mode = 'AUTO'

def setup_workbench_normals():
    scene = bpy.context.scene
    scene.render.engine = 'BLENDER_WORKBENCH'
    shading = scene.display.shading
    shading.light = 'MATCAP'
    shading.studio_light = 'check_normal+y.exr'
    shading.color_type = 'MATERIAL'
    scene.display.render_aa = '8'

def setup_workbench_rgb():
    scene = bpy.context.scene
    scene.render.engine = 'BLENDER_WORKBENCH'
    shading = scene.display.shading
    shading.light = 'STUDIO'
    shading.color_type = 'TEXTURE'
    shading.show_specular_highlight = True
    scene.display.render_aa = '8'

def setup_workbench_albedo():
    scene = bpy.context.scene
    scene.render.engine = 'BLENDER_WORKBENCH'
    shading = scene.display.shading
    shading.light = 'FLAT' # Pure Albedo
    shading.color_type = 'MATERIAL'
    shading.show_specular_highlight = False
    scene.display.render_aa = '8'

def setup_eevee_engine():
    """Ultra-fast EEVEE setup for data passes. Replaces slow Cycles."""
    scene = bpy.context.scene
    # Disable all heavy EEVEE features for maximum speed.
    try:
        scene.render.engine = 'BLENDER_EEVEE_NEXT'
    except TypeError:
        scene.render.engine = 'BLENDER_EEVEE'

    # Disable all heavy EEVEE features for maximum speed.
    # Uses try/except to support BOTH Blender 3.x and Blender 4.2+ (EEVEE Next)
    if hasattr(scene, "eevee"):
        try: scene.eevee.use_gtao = False
        except AttributeError: pass

        try: scene.eevee.use_bloom = False
        except AttributeError: pass

        try: scene.eevee.use_ssr = False
        except AttributeError: pass

        try: scene.eevee.use_volumetric = False
        except AttributeError: pass

        try: scene.eevee.use_shadows = False
        except AttributeError: pass

        try: scene.eevee.taa_render_samples = 8
        except AttributeError: pass

        # Blender 4.2+ Specific overheads
        try: scene.eevee.use_raytracing = False
        except AttributeError: pass

    # Important: Set view transform depending on what we render later
    scene.view_settings.view_transform = 'Standard'
    scene.view_settings.look = 'None'
    scene.view_settings.exposure = 0.0
    scene.view_settings.gamma = 1.0

def find_bsdf_recursive(node, stack=None):
    """Find a Principled BSDF by tracing from a node (usually Output) down."""
    if stack is None: stack = []

    if node.type in ['BSDF_PRINCIPLED', 'BSDF_GLOSSY']:
        return node, stack

    # If it's a group, go inside
    if node.type == 'GROUP' and node.node_tree:
        gout = next((n for n in node.node_tree.nodes if n.type == 'GROUP_OUTPUT'), None)
        if gout:
            for inp in gout.inputs:
                if inp.is_linked:
                    res = find_bsdf_recursive(inp.links[0].from_node, stack + [node])
                    if res: return res

    # Trace through mixers and modifiers
    if node.type in ['MIX_SHADER', 'ADD_SHADER', 'BRIGHT_CONTRAST', 'GAMMA', 'INVERT', 'CURVE_RGB', 'HUE_SAT', 'MIX']:
        for inp in node.inputs:
            if inp.is_linked and (not hasattr(inp, 'type') or inp.type in ['SHADER', 'RGBA', 'VALUE']):
                res = find_bsdf_recursive(inp.links[0].from_node, stack)
                if res: return res

    return None

def trace_socket_recursive(socket, stack):
    """Trace a socket back to its source, handling group boundaries and common nodes."""
    if not socket.is_linked:
        # Jump OUT of group
        if socket.node.type == 'GROUP_INPUT' and stack:
            parent_group = stack[-1]
            parent_socket = parent_group.inputs.get(socket.name)
            if not parent_socket:
                try:
                    idx = list(socket.node.outputs).index(socket)
                    if idx < len(parent_group.inputs):
                        parent_socket = parent_group.inputs[idx]
                except: pass

            if parent_socket:
                return trace_socket_recursive(parent_socket, stack[:-1])
        return {'type': 'VALUE', 'value': socket.default_value}

    link = socket.links[0]
    node = link.from_node

    if node.type == 'TEX_IMAGE':
        return {'type': 'TEX', 'node': node, 'socket': link.from_socket}

    if node.type == 'VALUE':
        return {'type': 'VALUE', 'value': node.outputs[0].default_value}

    if node.type in ['SEPARATE_RGB', 'SEPARATE_COLOR']:
        source = trace_socket_recursive(node.inputs[0], stack)
        if source['type'] == 'TEX':
            return {'type': 'TEX_CHANNEL', 'node': source['node'], 'channel': link.from_socket.name}

    # Jump INTO group
    if node.type == 'GROUP' and node.node_tree:
        group_out = link.from_socket
        for gout in [n for n in node.node_tree.nodes if n.type == 'GROUP_OUTPUT']:
            internal_socket = gout.inputs.get(group_out.name)
            if not internal_socket:
                try:
                    idx = list(node.outputs).index(group_out)
                    if idx < len(gout.inputs): internal_socket = gout.inputs[idx]
                except: pass
            if internal_socket:
                return trace_socket_recursive(internal_socket, stack + [node])

    # Detect Attribute nodes (common for vertex color data or packed PBR)
    if node.type == 'ATTRIBUTE':
        return {'type': 'ATTR', 'node': node, 'name': node.attribute_name}

    if node.type == 'NORMAL_MAP':
        for inp in node.inputs:
            if inp.name == 'Color' and inp.is_linked:
                return trace_socket_recursive(inp, stack)

    # Pass-through common modifiers (Math, Mix, Mapping, ColorRamp, etc.)
    # Be careful to follow the color/value inputs, not the Factor
    if node.type in ['MATH', 'MAPPING', 'VALTORGB', 'RGBTOBW', 'GAMMA', 'INVERT', 'BRIGHT_CONTRAST', 'HUE_SAT', 'RGB_CURVES']:
        for inp in node.inputs:
            if inp.is_linked:
                return trace_socket_recursive(inp, stack)

    if node.type in ['MIX', 'MIX_RGB']:
        for i in [1, 2]: # Skip factor at 0
            if node.inputs[i].is_linked:
                return trace_socket_recursive(node.inputs[i], stack)

    return {'type': 'VALUE', 'value': socket.default_value}

def create_pbr_material(orig_mat, mode='metallic', max_dim=1.0):
    mat_id = orig_mat.name if orig_mat else "NONE"
    mat_name = f"TMP_PBR_{mode}_{mat_id}"

    # For depth, we use a unique name per max_dim to avoid caching issues on different models
    if mode == 'depth':
        mat_name = f"TMP_PBR_depth_{max_dim:.3f}"

    if mat_name in bpy.data.materials:
        return bpy.data.materials[mat_name]

    mat = bpy.data.materials.new(name=mat_name)
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()

    node_out = nodes.new('ShaderNodeOutputMaterial')
    node_emit = nodes.new('ShaderNodeEmission')
    links.new(node_emit.outputs[0], node_out.inputs[0])

    if mode == 'metallic': val = (0.0, 0.0, 0.0, 1.0)
    elif mode == 'roughness': val = (0.5, 0.5, 0.5, 1.0)
    elif mode == 'albedo': val = (0.8, 0.8, 0.8, 1.0)
    elif mode == 'normals': val = (0.5, 0.5, 1.0, 1.0)
    else: val = (0.5, 0.5, 0.5, 1.0)

    if mode == 'depth':
        # Fast Workbench Depth using a Map Range in the shader
        dist = max_dim * 5 + 10
        near = dist - (max_dim * 0.7)
        far = dist + (max_dim * 0.7)

        node_camera = nodes.new('ShaderNodeCameraData')
        node_map = nodes.new('ShaderNodeMapRange')
        node_map.inputs['From Min'].default_value = near
        node_map.inputs['From Max'].default_value = far
        node_map.inputs['To Min'].default_value = 1.0
        node_map.inputs['To Max'].default_value = 0.0
        node_map.clamp = True
        links.new(node_camera.outputs['View Z Depth'], node_map.inputs[0])
        links.new(node_map.outputs[0], node_emit.inputs[0])
        return mat

    if orig_mat and orig_mat.use_nodes:
        bsdf_res = None
        mat_out = next((n for n in orig_mat.node_tree.nodes if n.type == 'OUTPUT_MATERIAL' and n.is_active_output), None)
        if not mat_out:
            mat_out = next((n for n in orig_mat.node_tree.nodes if n.type == 'OUTPUT_MATERIAL'), None)

        if mat_out and mat_out.inputs[0].is_linked:
            bsdf_res = find_bsdf_recursive(mat_out.inputs[0].links[0].from_node)

        if not bsdf_res:
            bsdf_node = next((n for n in orig_mat.node_tree.nodes if n.type == 'BSDF_PRINCIPLED'), None)
            if bsdf_node:
                bsdf_res = (bsdf_node, [])

        if bsdf_res:
            bsdf, stack = bsdf_res
            
            # Use reliable alias lists for robust lookup across different GLB exporters
            if mode == 'metallic': 
                aliases = ['metallic', 'metalness', 'specular', 'reflection']
            elif mode == 'roughness': 
                aliases = ['roughness', 'gloss', 'glossiness', 'smoothness']
            elif mode == 'albedo': 
                aliases = ['base color', 'color', 'diffuse', 'albedo']
            elif mode == 'normals': 
                aliases = ['normal', 'bump']
            else: 
                aliases = ['metallic']

            socket = None
            for alias in aliases:
                socket = next((s for s in bsdf.inputs if alias in s.name.lower()), None)
                if socket: break
                
            # Fallback if simply no socket found with those names but 'Color' exists
            if not socket and mode == 'albedo':
                socket = next((s for s in bsdf.inputs if 'color' in s.name.lower()), None)

            if socket:
                source = trace_socket_recursive(socket, stack)

                if source['type'] == 'TEX':
                    node_coord = nodes.new('ShaderNodeTexCoord')
                    new_tex = nodes.new('ShaderNodeTexImage')
                    new_tex.image = source['node'].image
                    new_tex.interpolation = source['node'].interpolation
                    links.new(node_coord.outputs['UV'], new_tex.inputs['Vector'])
                    links.new(new_tex.outputs[0], node_emit.inputs[0])
                    return mat

                elif source['type'] == 'TEX_CHANNEL':
                    node_coord = nodes.new('ShaderNodeTexCoord')
                    new_tex = nodes.new('ShaderNodeTexImage')
                    new_tex.image = source['node'].image
                    new_tex.interpolation = source['node'].interpolation

                    try: new_sep = nodes.new('ShaderNodeSeparateColor')
                    except: new_sep = nodes.new('ShaderNodeSeparateRGB')

                    links.new(node_coord.outputs['UV'], new_tex.inputs['Vector'])
                    links.new(new_tex.outputs[0], new_sep.inputs[0])

                    chan = source['channel']
                    out_socket = next((s for s in new_sep.outputs if s.name.startswith(chan[0])), new_sep.outputs[0])
                    links.new(out_socket, node_emit.inputs[0])
                    return mat

                elif source['type'] == 'ATTR':
                    new_attr = nodes.new('ShaderNodeAttribute')
                    new_attr.attribute_name = source['name']
                    links.new(new_attr.outputs['Color'], node_emit.inputs[0])
                    return mat

                elif source['type'] == 'VALUE':
                    val = source['value']
            else:
                if bsdf.type == 'BSDF_GLOSSY' and mode == 'metallic':
                    val = 1.0

    # Fallback to constant
    try:
        val = list(val)
    except TypeError:
        val = [val]

    if len(val) == 1: val = (val[0], val[0], val[0], 1.0)
    elif len(val) == 3: val = (val[0], val[1], val[2], 1.0)
    node_emit.inputs[0].default_value = val
    return mat

def get_bounds():
    """Ultra-fast bounding box calculation using C-level data."""
    min_c = mathutils.Vector((float('inf'), float('inf'), float('inf')))
    max_c = mathutils.Vector((float('-inf'), float('-inf'), float('-inf')))
    found = False

    for obj in bpy.data.objects:
        if obj.type != 'MESH' or obj.hide_render: continue
        found = True
        for corner in obj.bound_box:
            world_corner = obj.matrix_world @ mathutils.Vector(corner)
            for i in range(3):
                min_c[i] = min(min_c[i], world_corner[i])
                max_c[i] = max(max_c[i], world_corner[i])

    if not found:
        return mathutils.Vector((0,0,0)), 1.0

    center = (min_c + max_c) / 2
    max_dim = max(max_c - min_c)
    return center, max_dim

def prepare_mesh_objects(mat=None):
    meshes = [obj for obj in bpy.data.objects if obj.type == 'MESH']
    for obj in meshes:
        obj.hide_render = False
        obj.hide_viewport = False
        if mat:
            if not obj.material_slots:
                obj.data.materials.append(mat)
            else:
                for slot in obj.material_slots:
                    slot.material = mat
        elif not obj.data.materials:
            m = bpy.data.materials.new(name="Base")
            obj.data.materials.append(m)
    return meshes

def render_views(output_dir, cam, center, max_dim, prefix="", args=None):
    views = {
        'front':  (math.pi/2, 0, 0),
        'back':   (math.pi/2, 0, math.pi),
        'right':  (math.pi/2, 0, -math.pi/2), # Changed from +pi/2
        'left':   (math.pi/2, 0, math.pi/2),  # Changed from -pi/2
        'top':    (0, 0, 0),
        'bottom': (math.pi, 0, 0)
    }

    dist = max_dim * 5 + 10
    folder = os.path.join(output_dir, prefix)
    os.makedirs(folder, exist_ok=True)

    for name, rot in views.items():
        cam.rotation_euler = rot
        bpy.context.view_layer.update()
        q = mathutils.Euler(rot).to_quaternion()
        cam.location = center + (q @ mathutils.Vector((0, 0, dist)))

        bpy.context.view_layer.update()
        filepath = os.path.join(folder, f"{name}.png")

        # Robust Resume Check
        if args and not getattr(args, 'force', False):
            if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
                print(f"INFO: Skipping existing view: {prefix}/{name}")
                continue

        # Enforce Raw view transform specifically for data passes here right before render
        if prefix in ['metallic', 'roughness', 'depth']:
            bpy.context.scene.view_settings.view_transform = 'Raw'
        else:
            bpy.context.scene.view_settings.view_transform = 'Standard'

        bpy.context.scene.render.filepath = filepath
        bpy.ops.render.render(write_still=True)

def render_single_mesh(input_path, output_path, args):
    bpy.ops.wm.read_factory_settings(use_empty=True)

    if input_path == "TEST_CUBE": bpy.ops.mesh.primitive_cube_add(size=2)
    elif input_path == "TEST_MONKEY": bpy.ops.mesh.primitive_monkey_add(size=2)
    else:
        ext = os.path.splitext(input_path)[1].lower()
        
        # --- ADD TRY/EXCEPT BLOCK HERE ---
        try:
            if ext in ['.glb', '.gltf']:
                bpy.ops.import_scene.gltf(filepath=input_path)
            elif ext == '.obj':
                if hasattr(bpy.ops.wm, "obj_import"):
                    bpy.ops.wm.obj_import(filepath=input_path, forward_axis='NEGATIVE_Z', up_axis='Y')
                else:
                    bpy.ops.import_scene.obj(filepath=input_path, axis_forward='-Z', axis_up='Y')
            elif ext == '.stl':
                bpy.ops.import_mesh.stl(filepath=input_path, axis_forward='Y', axis_up='Z')
            else:
                print(f"Unsupported {ext} for {input_path}")
                return
        except Exception as e:
            print(f"WARNING: Skipping {input_path} due to import error: {e}")
            return
        # ---------------------------------

    bpy.context.view_layer.update()
    setup_common_settings(args.resolution, getattr(args, 'threads', 0))
    center, max_dim = get_bounds()

    for ob in bpy.data.objects:
        if ob.animation_data: ob.animation_data_clear()

    roots = [obj for obj in bpy.data.objects if obj.parent is None]
    bpy.ops.object.empty_add(type='PLAIN_AXES', location=center)
    centering_anchor = bpy.context.active_object
    centering_anchor.name = "Centering_Anchor"
    for obj in roots:
        mw = obj.matrix_world.copy()
        obj.parent = centering_anchor
        obj.matrix_parent_inverse = centering_anchor.matrix_world.inverted()
        obj.matrix_world = mw

    centering_anchor.location -= center
    bpy.context.view_layer.update()

    center = mathutils.Vector((0, 0, 0))
    cam_data = bpy.data.cameras.new("Cam")
    cam_obj = bpy.data.objects.new("Cam", cam_data)
    bpy.context.scene.collection.objects.link(cam_obj)
    bpy.context.scene.camera = cam_obj
    cam_data.type = 'ORTHO'
    cam_data.ortho_scale = max_dim / args.zoom

    orig_mats = {}
    for obj in bpy.data.objects:
        if obj.type == 'MESH':
            orig_mats[obj.name] = [m for m in obj.data.materials]

    def restore_original_materials():
        for obj_name, mats in orig_mats.items():
            obj = bpy.data.objects.get(obj_name)
            if obj and obj.type == 'MESH':
                obj.data.materials.clear()
                for m in mats: obj.data.materials.append(m)

    if args.depth:
        setup_eevee_engine()
        depth_mat = create_pbr_material(None, 'depth', max_dim=max_dim)
        prepare_mesh_objects(depth_mat)
        render_views(output_path, cam_obj, center, max_dim, prefix="depth", args=args)

    if getattr(args, 'rgb', False):
        setup_workbench_rgb()
        restore_original_materials()
        render_views(output_path, cam_obj, center, max_dim, prefix="rgb", args=args)

    for mode in ['albedo', 'normals', 'metallic', 'roughness']:
        if getattr(args, mode):
            setup_eevee_engine()
            restore_original_materials()
            for obj in bpy.data.objects:
                if obj.type == 'MESH':
                    if not obj.material_slots:
                        dummy = bpy.data.materials.new("dummy")
                        obj.data.materials.append(create_pbr_material(dummy, mode, max_dim=max_dim))
                    else:
                        for slot in obj.material_slots:
                            slot.material = create_pbr_material(slot.material, mode, max_dim=max_dim)
            if mode == 'normals': # The folder name is often expected as 'normals' but material pass should match
                render_views(output_path, cam_obj, center, max_dim, prefix="normals", args=args)
            elif mode == 'albedo':
                render_views(output_path, cam_obj, center, max_dim, prefix="albedo", args=args)
            elif mode == 'metallic':
                render_views(output_path, cam_obj, center, max_dim, prefix="metallic", args=args)
            else:
                render_views(output_path, cam_obj, center, max_dim, prefix=mode, args=args)


def run_worker(file_info):
    blender_path, script_path, display_name, tasks_list, args_dict = file_info

    is_python = "python" in os.path.basename(blender_path).lower()

    fd, task_file = tempfile.mkstemp(suffix=".json", prefix="blender_tasks_")
    with os.fdopen(fd, 'w') as f:
        json.dump(tasks_list, f)

    def cleanup():
        try: os.remove(task_file)
        except: pass
    atexit.register(cleanup)

    if is_python: cmd = [blender_path, script_path]
    else: cmd = [blender_path, "-b", "-P", script_path, "--"]

    cmd += [
        "--task-list", task_file,
        "--resolution", str(args_dict['resolution']),
        "--zoom", str(args_dict['zoom']),
        "--threads", str(args_dict.get('threads', 0)),
        "--parallel", "1"
    ]
    if args_dict.get('force'): cmd.append("--force")
    for k in ["normals", "depth", "rgb", "albedo", "metallic", "roughness"]:
        if args_dict.get(k): cmd.append(f"--{k}")

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print(f"ERROR: Worker chunk failed:\n{result.stderr}")
        return display_name, 0

    return display_name, len(tasks_list)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", help="Input GLB/OBJ path or directory")
    parser.add_argument("--output", help="Output direction")
    parser.add_argument("--task-list", help="Path to JSON file containing list of [input, output] pairs")
    parser.add_argument("--resolution", type=int, default=1024)
    parser.add_argument("--normals", action="store_true", help="Render normal maps")
    parser.add_argument("--depth", action="store_true", help="Render depth maps")
    parser.add_argument("--rgb", action="store_true", help="Render RGB images")
    parser.add_argument("--albedo", action="store_true", help="Render Albedo images")
    parser.add_argument("--metallic", action="store_true", help="Render Metallic maps")
    parser.add_argument("--roughness", action="store_true", help="Render Roughness maps")
    parser.add_argument("--parallel", type=int, default=1, help="Number of parallel Blender instances for batch mode")
    parser.add_argument("--threads", type=int, default=0, help="Internal threads per Blender instance (0 = Auto)")
    parser.add_argument("--zoom", type=float, default=1.0, help="Zoom level (higher = tighter)")
    parser.add_argument("--force", action="store_true", help="Force overwrite existing renders")
    parser.add_argument("--chunk-size", type=int, default=1, help="Number of files per worker session (reduces startup overhead)")
    parser.add_argument("--mem-limit", type=float, default=20.0, help="Target system RAM limit in GB (used for auto-parallelism)")

    is_blender = False
    try:
        import bpy
        is_blender = True
    except ImportError:
        pass

    if "--" in sys.argv:
        argv = sys.argv[sys.argv.index("--") + 1:]
    else:
        argv = sys.argv[1:]
    args = parser.parse_args(argv)

    if not (args.normals or args.depth or args.rgb or args.albedo or args.metallic or args.roughness):
        args.normals = args.depth = args.rgb = args.albedo = True

    if args.task_list:
        if not os.path.exists(args.task_list):
            print(f"ERROR: Task list not found: {args.task_list}")
            sys.exit(1)
        with open(args.task_list, 'r') as f:
            tasks = json.load(f)

        for in_path, out_dir in tasks:
            render_single_mesh(in_path, out_dir, args)
        return

    if args.input and os.path.isdir(args.input):
        supported_exts = ['.glb', '.gltf', '.obj', '.stl']
        files = [f for f in os.listdir(args.input) if os.path.splitext(f)[1].lower() in supported_exts]
        print(f"INFO: Found {len(files)} mesh files in {args.input}")

        if args.parallel > 1 or args.parallel == 1:
            import shutil
            is_bpy_module = False
            try:
                import bpy
                bp = bpy.app.binary_path
                if not bp or "blender" not in os.path.basename(bp).lower():
                    is_bpy_module = True
            except: pass

            blender_path = sys.executable if is_bpy_module else (shutil.which("blender") or "blender")
            script_path = os.path.abspath(__file__)
            args_dict = vars(args)

            enabled_maps = []
            for k in ["normals", "depth", "rgb", "albedo", "metallic", "roughness"]:
                if args_dict.get(k): enabled_maps.append(k)
            if not enabled_maps: enabled_maps = ["normals", "depth", "rgb", "albedo"]

            file_tasks = []
            for f in files:
                input_path = os.path.join(args.input, f)
                model_name = os.path.splitext(f)[0]
                output_dir = os.path.join(args.output, model_name)

                missing_maps = []
                for m in enabled_maps:
                    if not args.force:
                        if is_render_complete(os.path.join(output_dir, m), ['front','back','left','right','top','bottom']):
                            continue
                    missing_maps.append(m)

                if not missing_maps: continue
                file_tasks.append((input_path, output_dir))

            if not file_tasks:
                print("INFO: All models already rendered correctly. Nothing to do.")
                return

            if args.parallel == 1:
                try:
                    import multiprocessing
                    cores = multiprocessing.cpu_count()
                    used, total, _ = get_ram_usage()
                    target_ram = min(args.mem_limit, total if total > 0 else args.mem_limit)
                    suggested = int(min(target_ram / 1.5, cores * 4))
                    if suggested > 1:
                        print(f"INFO: Auto-scaling parallelism for {target_ram:.1f}GB RAM: Setting --parallel {suggested}")
                        args.parallel = suggested
                except: pass

            if args.parallel <= 1:
                for in_path, out_dir in file_tasks:
                    render_single_mesh(in_path, out_dir, args)
                return

            chunked_tasks = [file_tasks[i:i + args.chunk_size] for i in range(0, len(file_tasks), args.chunk_size)]
            print(f"INFO: Processing {len(file_tasks)} models in {len(chunked_tasks)} worker chunks using {args.parallel} instances.")

            start_batch = time.perf_counter()
            models_completed = 0
            total_models = len(file_tasks)

            with concurrent.futures.ProcessPoolExecutor(max_workers=args.parallel) as executor:
                worker_items = []
                for idx, chunk in enumerate(chunked_tasks):
                    name = f"Chunk_{idx}" if len(chunk) > 1 else os.path.basename(chunk[0][0])
                    worker_items.append((blender_path, script_path, name, chunk, args_dict))

                futures = [executor.submit(run_worker, item) for item in worker_items]
                for future in concurrent.futures.as_completed(futures):
                    res_name, count = future.result()
                    models_completed += count

                    elapsed = time.perf_counter() - start_batch
                    avg_speed = elapsed / max(1, models_completed)
                    remaining = avg_speed * (total_models - models_completed)
                    eta = str(timedelta(seconds=int(remaining)))

                    used_ram, _, ram_pct = get_ram_usage()
                    ram_str = f"RAM: {used_ram:.1f}G ({ram_pct:.0f}%)" if used_ram > 0 else ""
                    print(f"[PROGRESS] {models_completed}/{total_models} | {res_name} | Speed: {avg_speed:.2f}s/model | {ram_str} | ETA: {eta}")
        else:
            for f in files:
                input_path = os.path.join(args.input, f)
                model_name = os.path.splitext(f)[0]
                output_dir = os.path.join(args.output, model_name)
                render_single_mesh(input_path, output_dir, args)
    else:
        # Single file mode (Map-level parallelism)
        if args.parallel > 1 or args.parallel == 1:
            import shutil
            is_bpy_module = False
            try:
                import bpy
                if not bpy.app.binary_path or "blender" not in os.path.basename(bpy.app.binary_path).lower(): is_bpy_module = True
            except: pass

            blender_path = sys.executable if is_bpy_module else (shutil.which("blender") or "blender")
            script_path = os.path.abspath(__file__)

            tasks = []
            for k in ["normals", "depth", "rgb", "albedo", "metallic", "roughness"]:
                if getattr(args, k): tasks.append(k)
            if not tasks: tasks = ["normals", "depth", "rgb", "albedo"]

            worker_items = []
            for t in tasks:
                t_args = vars(args).copy()
                if not vars(args).get('force'):
                    if is_render_complete(os.path.join(args.output, t), ['front','back','left','right','top','bottom']):
                        continue
                worker_items.append((blender_path, script_path, f"{t}_pass", [(args.input, args.output)], t_args))

            if not worker_items:
                print("INFO: All map passes completed.")
                return

            if args.parallel == 1:
                try:
                    import multiprocessing
                    cores = multiprocessing.cpu_count()
                    used, total, _ = get_ram_usage()
                    target_ram = min(args.mem_limit, total if total > 0 else args.mem_limit)
                    suggested = int(min(target_ram / 1.1, cores * 4))
                    args.parallel = suggested
                except: pass

            if args.parallel <= 1:
                render_single_mesh(args.input, args.output, args)
                return

            print(f"INFO: Parallel passes: {len(worker_items)} (Workers: {args.parallel})")
            start_batch = time.perf_counter()
            completed = 0
            with concurrent.futures.ProcessPoolExecutor(max_workers=args.parallel) as executor:
                futures = [executor.submit(run_worker, item) for item in worker_items]
                for future in concurrent.futures.as_completed(futures):
                    completed += 1
                    res, _ = future.result()
                    elapsed = time.perf_counter() - start_batch
                    avg_speed = elapsed / completed
                    remaining = avg_speed * (len(worker_items) - completed)
                    print(f"[PROGRESS] {completed}/{len(worker_items)} | {res} | ETA: {str(timedelta(seconds=int(remaining)))}")
        else:
            if not is_blender:
                 print("ERROR: Single file mode must be run in Blender.")
                 sys.exit(1)
            render_single_mesh(args.input, args.output, args)

if __name__ == "__main__":
    main()
