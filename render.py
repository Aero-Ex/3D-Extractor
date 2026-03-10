import os
import argparse
import math
import sys
import concurrent.futures
import subprocess
try:
    import bpy
    import mathutils
except ImportError:
    # Allow running as a master process to spawn blender workers
    pass

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
    scene.render.image_settings.file_format = 'PNG'
    scene.render.image_settings.color_mode = 'RGBA'
    scene.render.image_settings.color_depth = '16'
    scene.render.image_settings.compression = 0 
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
    scene.display.render_aa = '32' # Ultra AA

def setup_workbench_rgb():
    scene = bpy.context.scene
    scene.render.engine = 'BLENDER_WORKBENCH'
    shading = scene.display.shading
    shading.light = 'STUDIO'
    shading.color_type = 'TEXTURE'
    shading.show_specular_highlight = True
    scene.display.render_aa = '32' # Ultra AA

def setup_workbench_albedo():
    scene = bpy.context.scene
    scene.render.engine = 'BLENDER_WORKBENCH'
    shading = scene.display.shading
    shading.light = 'FLAT' # Pure Albedo
    shading.color_type = 'TEXTURE'
    shading.show_specular_highlight = False
    scene.display.render_aa = '32' # Ultra AA

def setup_cycles_engine():
    scene = bpy.context.scene
    scene.render.engine = 'CYCLES'
    if hasattr(scene, "cycles"):
        # Auto-detect GPU
        prefs = bpy.context.preferences
        cprefs = prefs.addons['cycles'].preferences
        
        # Look for OptiX first, then CUDA
        best_type = 'NONE'
        for dt in ['OPTIX', 'CUDA', 'OPENCL', 'METAL']:
            try:
                cprefs.get_devices_for_type(dt)
                best_type = dt
                break
            except: pass
            
        if best_type != 'NONE':
            cprefs.compute_device_type = best_type
            best_device = None
            for device in cprefs.devices:
                if device.type == best_type or (best_type == 'OPTIX' and device.type == 'CUDA'):
                    device.use = True
                    best_device = device
            
            if best_device:
                scene.cycles.device = 'GPU'
                print(f"INFO: Cycles using GPU ({best_type}): {best_device.name}")
            else:
                scene.cycles.device = 'CPU'
        else:
            scene.cycles.device = 'CPU'
            
        scene.cycles.samples = 1
        scene.cycles.use_denoising = False
        scene.cycles.max_bounces = 0
    
    # CRITICAL: Force linear data output for metallic/roughness/depth
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

def create_pbr_material(orig_mat, mode='metallic'):
    mat_id = orig_mat.name if orig_mat else "NONE"
    mat_name = f"TMP_PBR_{mode}_{mat_id}"
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
    
    val = 0.0 if mode == 'metallic' else 0.5
    
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
            input_name = "Metallic" if mode == 'metallic' else "Roughness"
            # Fuzzy match input name
            socket = next((s for s in bsdf.inputs if input_name.lower() in s.name.lower()), None)
            
            if socket:
                source = trace_socket_recursive(socket, stack)
                
                if source['type'] == 'TEX':
                    node_coord = nodes.new('ShaderNodeTexCoord')
                    new_tex = nodes.new('ShaderNodeTexImage')
                    new_tex.image = source['node'].image
                    new_tex.interpolation = source['node'].interpolation
                    links.new(node_coord.outputs['UV'], new_tex.inputs['Vector'])
                    links.new(new_tex.outputs[0], node_emit.inputs[0])
                    print(f"DEBUG: [{mat_id}] Found Texture for {mode}: {new_tex.image.name if new_tex.image else 'None'}")
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
                    print(f"DEBUG: [{mat_id}] Found Packed channel {chan} for {mode}: {new_tex.image.name if new_tex.image else 'None'}")
                    return mat
                
                elif source['type'] == 'ATTR':
                    new_attr = nodes.new('ShaderNodeAttribute')
                    new_attr.attribute_name = source['name']
                    links.new(new_attr.outputs['Color'], node_emit.inputs[0])
                    print(f"DEBUG: [{mat_id}] Found Attribute for {mode}: {source['name']}")
                    return mat
                
                elif source['type'] == 'VALUE':
                    val = source['value']
                    print(f"DEBUG: [{mat_id}] Found constant value for {mode}: {val}")
            else:
                # Fallback if no specific socket found on a Glossy/Custom node
                if bsdf.type == 'BSDF_GLOSSY' and mode == 'metallic':
                    val = 1.0 # Glossy is effectively 100% metallic data in this context
                print(f"DEBUG: [{mat_id}] No '{input_name}' socket found on {bsdf.type}")
        else:
            print(f"DEBUG: [{mat_id}] No BSDF found in material tree")

    # Fallback to constant
    if isinstance(val, (list, tuple)): val = val[0]
    node_emit.inputs[0].default_value = (val, val, val, 1.0)
    if not (orig_mat and orig_mat.use_nodes):
        print(f"DEBUG: [{mat_id}] Using default value {val} (no material/nodes)")
    return mat

def setup_cycles_depth(center, max_dim):
    setup_cycles_engine()
    scene = bpy.context.scene

    # Create special Depth Material
    mat = bpy.data.materials.new(name="UltraDepth")
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()
    
    # Calculate ranges (Tighter for maximum depth accuracy)
    # Calculate ranges (Slightly wider for character safety)
    dist = max_dim * 5 + 10
    near = dist - (max_dim * 0.7)
    far = dist + (max_dim * 0.7)
    
    print(f"INFO: Depth Range [{near:.3f} to {far:.3f}] for max accuracy.")
    
    node_camera = nodes.new('ShaderNodeCameraData')
    node_map = nodes.new('ShaderNodeMapRange')
    node_map.inputs['From Min'].default_value = near
    node_map.inputs['From Max'].default_value = far
    node_map.inputs['To Min'].default_value = 1.0 # Nearer = White (Standard for AI)
    node_map.inputs['To Max'].default_value = 0.0 # Farther = Black
    node_map.clamp = True
    
    node_emission = nodes.new('ShaderNodeEmission')
    node_output = nodes.new('ShaderNodeOutputMaterial')
    
    links.new(node_camera.outputs['View Z Depth'], node_map.inputs[0])
    links.new(node_map.outputs[0], node_emission.inputs['Color'])
    links.new(node_emission.outputs['Emission'], node_output.inputs['Surface'])
    
    # Force Standard view transform for data-like depth
    scene.view_settings.view_transform = 'Standard'
    scene.view_settings.exposure = 0.0
    scene.view_settings.gamma = 1.0
    
    return mat

def prepare_mesh_objects(mat=None):
    meshes = [obj for obj in bpy.data.objects if obj.type == 'MESH']
    for obj in meshes:
        obj.hide_render = False
        obj.hide_viewport = False
        if mat:
            # Replace materials in existing slots to preserve face mapping
            if not obj.material_slots:
                obj.data.materials.append(mat)
            else:
                for slot in obj.material_slots:
                    slot.material = mat
        elif not obj.data.materials:
            m = bpy.data.materials.new(name="Base")
            obj.data.materials.append(m)
    return meshes

def get_bounds():
    depsgraph = bpy.context.evaluated_depsgraph_get()
    min_c = mathutils.Vector((float('inf'), float('inf'), float('inf')))
    max_c = mathutils.Vector((float('-inf'), float('-inf'), float('-inf')))
    found = False
    
    meshes = [obj for obj in bpy.data.objects if obj.type == 'MESH']
    
    for obj in meshes:
        if obj.hide_render or not obj.visible_get(): continue
            
        eval_obj = obj.evaluated_get(depsgraph)
        mesh = eval_obj.to_mesh()
        
        if len(mesh.vertices) == 0:
            eval_obj.to_mesh_clear()
            continue
            
        found = True
        mat = obj.matrix_world
        for v in mesh.vertices:
            world_v = mat @ v.co
            for i in range(3):
                min_c[i] = min(min_c[i], world_v[i])
                max_c[i] = max(max_c[i], world_v[i])
        
        eval_obj.to_mesh_clear()

    if not found:
        return mathutils.Vector((0,0,0)), 1.0
        
    center = (min_c + max_c) / 2
    max_dim = max(max_c - min_c)
    return center, max_dim

def render_views(output_dir, cam, center, max_dim, prefix="", args=None):
    views = {
        'front':  (math.pi/2, 0, 0),           # -Y (Looking towards +Y)
        'back':   (math.pi/2, 0, math.pi),       # +Y (Looking towards -Y)
        'left':   (math.pi/2, 0, -math.pi/2),    # -X (Looking towards +X)
        'right':  (math.pi/2, 0, math.pi/2),     # +X (Looking towards -X)
        'top':    (0, 0, 0),                   # +Z (Looking towards -Z)
        'bottom': (math.pi, 0, 0)              # -Z (Looking towards +Z)
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

        bpy.context.scene.render.filepath = filepath
        bpy.ops.render.render(write_still=True)
        print(f"INFO: Rendered {prefix} {name}")

def render_single_mesh(input_path, output_path, args):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    
    if input_path == "TEST_CUBE": bpy.ops.mesh.primitive_cube_add(size=2)
    elif input_path == "TEST_MONKEY": bpy.ops.mesh.primitive_monkey_add(size=2)
    else:
        ext = os.path.splitext(input_path)[1].lower()
        if ext in ['.glb', '.gltf']: bpy.ops.import_scene.gltf(filepath=input_path)
        elif ext == '.obj': bpy.ops.import_scene.obj(filepath=input_path)
        elif ext == '.stl': bpy.ops.import_mesh.stl(filepath=input_path)
        else: return print(f"Unsupported {ext} for {input_path}")
    
    # Crucial: Force update to calculate matrices after import
    bpy.context.view_layer.update()

    setup_common_settings(args.resolution, getattr(args, 'threads', 0))
    # 5. Iterative Centering Loop (Ensures precise 0,0,0 alignment for Rigs/Characters)
    centering_anchor = None
    final_max_dim = 1.0

    for i in range(3):
        center, max_dim = get_bounds()
        final_max_dim = max_dim 
        
        if i > 0 and center.length < 0.0001: 
            print(f"INFO: Centering converged on pass {i+1}")
            break
            
        print(f"DEBUG: Centering Pass {i+1}, shifting by {-center}")
        
        if centering_anchor is None:
            # First pass: Clear all animations to prevent offsets during render
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

    max_dim = final_max_dim
    center = mathutils.Vector((0, 0, 0))

    center = mathutils.Vector((0, 0, 0))
    
    cam_data = bpy.data.cameras.new("Cam")
    cam_obj = bpy.data.objects.new("Cam", cam_data)
    bpy.context.scene.collection.objects.link(cam_obj)
    bpy.context.scene.camera = cam_obj
    cam_data.type = 'ORTHO'
    cam_data.ortho_scale = max_dim / args.zoom
    
    # Store original materials to restore them later for RGB
    orig_mats = {}
    for obj in bpy.data.objects:
        if obj.type == 'MESH':
            orig_mats[obj.name] = [m for m in obj.data.materials]

    # 1. NORMALS (Workbench)
    if args.normals:
        setup_workbench_normals()
        prepare_mesh_objects()
        render_views(output_path, cam_obj, center, max_dim, prefix="normals", args=args)
    
    # 2. DEPTH (Cycles Shader - More precise for 16-bit)
    if args.depth:
        depth_mat = setup_cycles_depth(center, max_dim)
        prepare_mesh_objects(depth_mat)
        render_views(output_path, cam_obj, center, max_dim, prefix="depth", args=args)

    prepare_mesh_objects() 
    
    # 3. RGB (Workbench)
    if args.rgb:
        setup_workbench_rgb()
        # Restore original materials
        for obj_name, mats in orig_mats.items():
            obj = bpy.data.objects.get(obj_name)
            if obj and obj.type == 'MESH':
                obj.data.materials.clear()
                for m in mats:
                    obj.data.materials.append(m)
        render_views(output_path, cam_obj, center, max_dim, prefix="rgb", args=args)

    # 4. ALBEDO (Workbench Flat)
    if args.albedo:
        setup_workbench_albedo()
        # Restore original materials
        for obj_name, mats in orig_mats.items():
            obj = bpy.data.objects.get(obj_name)
            if obj and obj.type == 'MESH':
                obj.data.materials.clear()
                for m in mats:
                    obj.data.materials.append(m)
        render_views(output_path, cam_obj, center, max_dim, prefix="albedo", args=args)

    # 5. METALLIC (Cycles Data)
    if args.metallic:
        print("INFO: Starting Metallic Rendering Pass")
        setup_cycles_engine()
        for obj in bpy.data.objects:
            if obj.type == 'MESH':
                if not obj.material_slots:
                    dummy = bpy.data.materials.new("dummy")
                    obj.data.materials.append(create_pbr_material(dummy, 'metallic'))
                else:
                    for slot in obj.material_slots:
                        slot.material = create_pbr_material(slot.material, 'metallic')
        render_views(output_path, cam_obj, center, max_dim, prefix="metallic", args=args)

    # 6. ROUGHNESS (Cycles Data)
    if args.roughness:
        print("INFO: Starting Roughness Rendering Pass")
        setup_cycles_engine()
        for obj in bpy.data.objects:
            if obj.type == 'MESH':
                if not obj.material_slots:
                    dummy = bpy.data.materials.new("dummy")
                    obj.data.materials.append(create_pbr_material(dummy, 'roughness'))
                else:
                    for slot in obj.material_slots:
                        slot.material = create_pbr_material(slot.material, 'roughness')
        render_views(output_path, cam_obj, center, max_dim, prefix="roughness", args=args)


def run_worker(file_info):
    blender_path, script_path, fname, in_path, out_dir, args_dict = file_info
    
    # Check if we should use 'blender -b -P' or just 'python'
    # If the path looks like a python interpreter, we don't need -b -P
    is_python = "python" in os.path.basename(blender_path).lower()
    
    if is_python:
        cmd = [blender_path, script_path]
    else:
        cmd = [blender_path, "-b", "-P", script_path, "--"]
        
    cmd += [
        "--input", in_path,
        "--output", out_dir,
        "--resolution", str(args_dict['resolution']),
        "--zoom", str(args_dict['zoom']),
        "--threads", str(args_dict.get('threads', 0)),
        "--parallel", "1" # Don't nest
    ]
    if args_dict.get('normals'): cmd.append("--normals")
    if args_dict.get('depth'): cmd.append("--depth")
    if args_dict.get('rgb'): cmd.append("--rgb")
    if args_dict.get('albedo'): cmd.append("--albedo")
    if args_dict.get('metallic'): cmd.append("--metallic")
    if args_dict.get('roughness'): cmd.append("--roughness")
    if args_dict.get('force'): cmd.append("--force")
    
    print(f"INFO: Worker starting: {fname}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"ERROR: Worker failed for {fname}:\n{result.stderr}")
    return fname

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--resolution", type=int, default=2048)
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
    
    # Check if we are being called by Blender or as a standalone python script
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
    
    # Default to all if none specified
    if not (args.normals or args.depth or args.rgb or args.albedo or args.metallic or args.roughness):
        args.normals = args.depth = args.rgb = args.albedo = True
    
    if os.path.isdir(args.input):
        # Batch mode (File-level parallelism)
        supported_exts = ['.glb', '.gltf', '.obj', '.stl']
        files = [f for f in os.listdir(args.input) if os.path.splitext(f)[1].lower() in supported_exts]
        print(f"INFO: Found {len(files)} mesh files in {args.input}")
        
        if args.parallel > 1:
            print(f"INFO: Spawning {args.parallel} workers for batch file processing...")
            # Detect if we are in a 'bpy' module environment
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
            import copy
            args_dict = vars(args)

            # Determine enabled maps
            enabled_maps = []
            for k in ["normals", "depth", "rgb", "albedo", "metallic", "roughness"]:
                if args_dict.get(k): enabled_maps.append(k)
            if not enabled_maps: enabled_maps = ["normals", "depth", "rgb", "albedo"]

            worker_items = []
            for f in files:
                input_path = os.path.join(args.input, f)
                model_name = os.path.splitext(f) [0]
                output_dir = os.path.join(args.output, model_name)
                
                # Flatten tasks: each map of each file is a separate worker task
                # This ensures we saturate all threads even with few files
                for m in enabled_maps:
                    m_args = copy.deepcopy(args_dict)
                    for k in ["normals", "depth", "rgb", "albedo", "metallic", "roughness"]:
                        m_args[k] = (k == m)
                    
                    # Robust Batch Skip
                    if not args_dict.get('force'):
                        expected_views = ['front', 'back', 'left', 'right', 'top', 'bottom']
                        prefix_dir = os.path.join(output_dir, m)
                        if is_render_complete(prefix_dir, expected_views):
                            print(f"INFO: Skipping completed pass: {model_name} -> {m}")
                            continue

                    worker_items.append((blender_path, script_path, f"{model_name}_{m}", input_path, output_dir, m_args))
                
            with concurrent.futures.ProcessPoolExecutor(max_workers=args.parallel) as executor:
                futures = [executor.submit(run_worker, item) for item in worker_items]
                for future in concurrent.futures.as_completed(futures):
                    print(f"INFO: Completed batch task: {future.result()}")
        else:
            for f in files:
                input_path = os.path.join(args.input, f)
                model_name = os.path.splitext(f)[0]
                output_dir = os.path.join(args.output, model_name)
                print(f"INFO: Processing {f} -> {output_dir}")
                render_single_mesh(input_path, output_dir, args)
    else:
        # Single file mode (Map-level parallelism)
        if args.parallel > 1:
            print(f"INFO: Spawning {args.parallel} workers for map-level parallelism...")
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
            
            tasks = []
            if args.normals: tasks.append("normals")
            if args.depth: tasks.append("depth")
            if args.rgb: tasks.append("rgb")
            if args.albedo: tasks.append("albedo")
            if args.metallic: tasks.append("metallic")
            if args.roughness: tasks.append("roughness")
            
            if not tasks:
                tasks = ["normals", "depth", "rgb", "albedo"]
                
            worker_items = []
            for t in tasks:
                # We reuse run_worker logic by creating a custom args_dict
                t_args = vars(args).copy()
                # Disable all, enable only this one
                for k in ["normals", "depth", "rgb", "albedo", "metallic", "roughness"]:
                    t_args[k] = (k == t)
                
                model_name = os.path.splitext(os.path.basename(args.input))[0]
                
                # Robust Map-Level Skip
                if not vars(args).get('force'):
                    expected_views = ['front', 'back', 'left', 'right', 'top', 'bottom']
                    prefix_dir = os.path.join(args.output, t)
                    if is_render_complete(prefix_dir, expected_views):
                        print(f"INFO: Skipping completed map pass: {t}")
                        continue

                worker_items.append((blender_path, script_path, f"{t}_pass", args.input, args.output, t_args))

            with concurrent.futures.ProcessPoolExecutor(max_workers=args.parallel) as executor:
                futures = [executor.submit(run_worker, item) for item in worker_items]
                for future in concurrent.futures.as_completed(futures):
                    print(f"INFO: Completed map pass {future.result()}")
        else:
            if not is_blender:
                 print("ERROR: Single file mode must be run inside Blender (blender -b -P script.py -- ...)")
                 sys.exit(1)
            render_single_mesh(args.input, args.output, args)

if __name__ == "__main__":
    main()
