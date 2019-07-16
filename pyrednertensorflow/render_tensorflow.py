from __future__ import absolute_import, division, print_function
from typing import List, Set, Dict, Tuple, Optional, Callable, Union
import tensorflow as tf
import numpy as np
import redner
import pyrednertensorflow as pyredner
import time
import weakref
import pdb

__EMPTY_TENSOR = tf.constant([])
# There is a bias-variance trade off in the backward pass.
# If the forward pass and the backward pass are correlated
# the gradients are biased for L2 loss.
# (E[d/dx(f(x) - y)^2] = E[(f(x) - y) d/dx f(x)])
#                      = E[f(x) - y] E[d/dx f(x)]
# The last equation only holds when f(x) and d/dx f(x) are independent.
# It is usually better to use the unbiased one, but we left it as an option here
use_correlated_random_number = False
def set_use_correlated_random_number(v):
    global use_correlated_random_number
    use_gpu = v

def get_use_correlated_random_number():
    global use_correlated_random_number
    return use_correlated_random_number

def get_tensor_dimension(t):
    """Return dimension of the TF tensor in Int

    `get_shape()` returns `TensorShape`.
    
    """
    return len(t.get_shape())

def is_empty_tensor(tensor):
    return  tf.equal(tf.size(tensor), 0)
    

class Context: pass

__ctx = Context()
print_timing = True

def serialize_scene(scene: pyredner.Scene,
                    num_samples: int,
                    max_bounces: int,
                    channels = [redner.channels.radiance],
                    sampler_type = redner.SamplerType.independent,
                    use_primary_edge_sampling = True,
                    use_secondary_edge_sampling = True) -> List:
    """
        Given a PyRedner scene & rendering options, convert them to a linear list of argument,
        so that we can use it in TensorFlow.

        Keyword arguments:
        scene -- A pyredner.Scene
        num_samples -- Number of samples per pixel for forward and backward passes,
                        can be an integer or a tuple of 2 integers.
        max_bounces -- Number of bounces for global illumination, 1 means direct lighting only.
        channels -- A list of channels that should present in the output image.
                    Following channels are supported:
                        redner.channels.radiance,
                        redner.channels.alpha,
                        redner.channels.depth,
                        redner.channels.position,
                        redner.channels.geometry_normal,
                        redner.channels.shading_normal,
                        redner.channels.uv,
                        redner.channels.diffuse_reflectance,
                        redner.channels.specular_reflectance,
                        redner.channels.roughness,
                        redner.channels.shape_id,
                        redner.channels.material_id
                    All channels, except for shape id and material id, are differentiable.
        sampler_type -- Which sampling pattern to use.
                        See Chapter 7 of the PBRT book for an explanation of the difference between
                        different samplers.
                        http://www.pbr-book.org/3ed-2018/Sampling_and_Reconstruction.html
                        Following samplers are supported:
                            redner.SamplerType.independent
                            redner.SamplerType.sobol
        use_primary_edge_sampling -- A boolean
        use_secondary_edge_sampling -- A boolean

        tf.custom_gradient in Tensorflow can take only tf.Tensor objects as arguments.
        Hense, map `None` to False boolean tensors
    """
    global __ctx
    ctx = __ctx

    ctx.pyredner_scene = scene

    cam = scene.camera
    num_shapes = len(scene.shapes)
    num_materials = len(scene.materials)
    num_lights = len(scene.area_lights)
    num_channels = len(channels)

    for light_id, light in enumerate(scene.area_lights):
        scene.shapes[light.shape_id].light_id = light_id

    # NOTE: 
    args = []
    args.append(tf.constant(num_shapes))    # 1
    args.append(tf.constant(num_materials)) # 2
    args.append(tf.constant(num_lights))    # 3
    args.append(cam.position)               # 4
    args.append(cam.look_at)                # 5
    args.append(cam.up)                     # 6
    args.append(cam.ndc_to_cam)             # 7
    args.append(cam.cam_to_ndc)             # 8
    args.append(tf.constant(cam.clip_near)) # 9
    args.append(tf.constant(cam.resolution))# 10
    args.append(pyredner.RednerCameraType.asTensor(cam.camera_type))   # 11
    for shape in scene.shapes:   
        args.append(shape.vertices)   # 12
        args.append(shape.indices)    # 13
        if shape.uvs is None:         # 14
            args.append(__EMPTY_TENSOR)
        else:
            args.append(shape.uvs) 
        if shape.normals is None:     # 15
            args.append(__EMPTY_TENSOR)
        else:
            args.append(shape.normals) 
        args.append(tf.constant(shape.material_id)) # 16
        args.append(tf.constant(shape.light_id))    # 17
    for material in scene.materials: 
        args.append(material.diffuse_reflectance.mipmap)    # 18
        args.append(material.diffuse_reflectance.uv_scale)  # 19
        args.append(material.specular_reflectance.mipmap)   # 20
        args.append(material.specular_reflectance.uv_scale) # 21
        args.append(material.roughness.mipmap)              # 22
        args.append(material.roughness.uv_scale)            # 23
        args.append(tf.constant(material.two_sided))        # 24
    for light in scene.area_lights:  
        args.append(tf.constant(light.shape_id))    #
        args.append(light.intensity)    #
        args.append(tf.constant(light.two_sided))    #
    if scene.envmap is not None:
        args.append(scene.envmap.values.mipmap)    # 25
        args.append(scene.envmap.values.uv_scale)    #
        args.append(scene.envmap.env_to_world)    #
        args.append(scene.envmap.world_to_env)    #
        args.append(scene.envmap.sample_cdf_ys)    #
        args.append(scene.envmap.sample_cdf_xs)    #
        args.append(scene.envmap.pdf_norm)    #
    else:
        args.append(__EMPTY_TENSOR)    #
        args.append(__EMPTY_TENSOR)    #
        args.append(__EMPTY_TENSOR)    #
        args.append(__EMPTY_TENSOR)    #
        args.append(__EMPTY_TENSOR)    #
        args.append(__EMPTY_TENSOR)    #
        args.append(__EMPTY_TENSOR)    #

    args.append(tf.constant(num_samples))    #
    args.append(tf.constant(max_bounces))    #
    args.append(tf.constant(num_channels))    #
    for ch in channels: # 1
        args.append(pyredner.RednerChannels.asTensor(ch))    #

    args.append(pyredner.RednerSamplerType.asTensor(sampler_type))    #
    args.append(tf.constant(use_primary_edge_sampling))    #
    args.append(tf.constant(use_secondary_edge_sampling))    #
    return args

def forward(seed:int, *args):
    """
        Forward rendering pass: given a scene and output an image.
    """
    global __ctx
    ctx = __ctx

    # Unpack arguments
    current_index = 0
    num_shapes = int(args[current_index])
    current_index += 1
    num_materials = int(args[current_index])
    current_index += 1
    num_lights = int(args[current_index])
    current_index += 1

    # Camera arguments
    cam_position = args[current_index]
    current_index += 1
    cam_look_at = args[current_index]
    current_index += 1
    cam_up = args[current_index]
    current_index += 1
    ndc_to_cam = args[current_index]
    current_index += 1
    cam_to_ndc = args[current_index]
    current_index += 1
    clip_near = float(args[current_index])
    current_index += 1
    resolution = args[current_index].numpy() # Tuple[int, int]
    current_index += 1
    camera_type = pyredner.RednerCameraType.asCameraType(args[current_index]) # FIXME: Map to custom type
    current_index += 1

    camera = redner.Camera(resolution[1],
                           resolution[0],
                           redner.float_ptr(pyredner.data_ptr(cam_position)),
                           redner.float_ptr(pyredner.data_ptr(cam_look_at)),
                           redner.float_ptr(pyredner.data_ptr(cam_up)),
                           redner.float_ptr(pyredner.data_ptr(ndc_to_cam)),
                           redner.float_ptr(pyredner.data_ptr(cam_to_ndc)),
                           clip_near,
                           camera_type)

    shapes = []
    for i in range(num_shapes):
        vertices = args[current_index]
        current_index += 1
        indices = args[current_index]
        current_index += 1
        uvs = args[current_index]
        current_index += 1
        normals = args[current_index]
        current_index += 1
        material_id = int(args[current_index])
        current_index += 1
        light_id = int(args[current_index])
        current_index += 1
        shapes.append(redner.Shape(\
            redner.float_ptr(pyredner.data_ptr(vertices)),
            redner.int_ptr(pyredner.data_ptr(indices)),
            redner.float_ptr(pyredner.data_ptr(uvs) if uvs is not None else 0),
            redner.float_ptr(pyredner.data_ptr(normals) if normals is not None else 0),
            int(vertices.shape[0]),
            int(indices.shape[0]),
            material_id,
            light_id))


    materials = []
    for i in range(num_materials):
        diffuse_reflectance = args[current_index]
        current_index += 1
        diffuse_uv_scale = args[current_index]
        current_index += 1
        specular_reflectance = args[current_index]
        current_index += 1
        specular_uv_scale = args[current_index]
        current_index += 1
        roughness = args[current_index]
        current_index += 1
        roughness_uv_scale = args[current_index]
        current_index += 1
        two_sided = bool(args[current_index])
        current_index += 1
        
        if get_tensor_dimension(diffuse_reflectance) == 1:
            diffuse_reflectance = redner.Texture3(\
                redner.float_ptr(pyredner.data_ptr(diffuse_reflectance)), 0, 0, 0,
                redner.float_ptr(pyredner.data_ptr(diffuse_uv_scale)))
        else:
            diffuse_reflectance = redner.Texture3(\
                redner.float_ptr(pyredner.data_ptr(diffuse_reflectance)),
                int(diffuse_reflectance.shape[2]), # width
                int(diffuse_reflectance.shape[1]), # height
                int(diffuse_reflectance.shape[0]), # num levels
                redner.float_ptr(pyredner.data_ptr(diffuse_uv_scale)))
        if get_tensor_dimension(specular_reflectance) == 1:
            specular_reflectance = redner.Texture3(\
                redner.float_ptr(pyredner.data_ptr(specular_reflectance)), 0, 0, 0,
                redner.float_ptr(pyredner.data_ptr(specular_uv_scale)))
        else:
            specular_reflectance = redner.Texture3(\
                redner.float_ptr(pyredner.data_ptr(specular_reflectance)),
                int(specular_reflectance.shape[2]), # width
                int(specular_reflectance.shape[1]), # height
                int(specular_reflectance.shape[0]), # num levels
                redner.float_ptr(pyredner.data_ptr(specular_uv_scale)))
        if get_tensor_dimension(roughness) == 1:
            roughness = redner.Texture1(\
                redner.float_ptr(pyredner.data_ptr(roughness)), 0, 0, 0,
                redner.float_ptr(pyredner.data_ptr(roughness_uv_scale)))
        else:
            assert(get_tensor_dimension(roughness) == 4)
            roughness = redner.Texture1(\
                redner.float_ptr(pyredner.data_ptr(roughness)),
                int(roughness.shape[2]), # width
                int(roughness.shape[1]), # height
                int(roughness.shape[0]), # num levels
                redner.float_ptr(pyredner.data_ptr(roughness_uv_scale)))
        materials.append(redner.Material(\
            diffuse_reflectance,
            specular_reflectance,
            roughness,
            two_sided))

    area_lights = []
    for i in range(num_lights):
        shape_id = int(args[current_index])
        current_index += 1
        intensity = args[current_index]
        current_index += 1
        two_sided = bool(args[current_index])
        current_index += 1

        area_lights.append(redner.AreaLight(
            shape_id,
            redner.float_ptr(pyredner.data_ptr(intensity)),
            two_sided))

    envmap = None
    if not is_empty_tensor(args[current_index]):
        print(">>> You have envmap")
        values = args[current_index]
        current_index += 1
        envmap_uv_scale = args[current_index]
        current_index += 1
        env_to_world = args[current_index]
        current_index += 1
        world_to_env = args[current_index]
        current_index += 1
        sample_cdf_ys = args[current_index]
        current_index += 1
        sample_cdf_xs = args[current_index]
        current_index += 1
        pdf_norm = float(args[current_index])
        current_index += 1

        assert isinstance(pdf_norm, float)
        values = redner.Texture3(
            redner.float_ptr(pyredner.data_ptr(values)),
            int(values.shape[2]), # width
            int(values.shape[1]), # height
            int(values.shape[0]), # num levels
            redner.float_ptr(pyredner.data_ptr(envmap_uv_scale)))
        envmap = redner.EnvironmentMap(\
            values,
            redner.float_ptr(pyredner.data_ptr(env_to_world)),
            redner.float_ptr(pyredner.data_ptr(world_to_env)),
            redner.float_ptr(pyredner.data_ptr(sample_cdf_ys)),
            redner.float_ptr(pyredner.data_ptr(sample_cdf_xs)),
            pdf_norm)

    else:
        current_index += 7

    # Options
    num_samples = int(args[current_index])
    current_index += 1
    max_bounces = int(args[current_index])
    current_index += 1

    __num_channels = int(args[current_index])
    current_index += 1

    channels = []
    for _ in range(__num_channels):
        ch = args[current_index]
        ch = pyredner.RednerChannels.asChannel(ch)
        channels.append(ch)
        current_index += 1

    sampler_type = args[current_index]
    sampler_type = pyredner.RednerSamplerType.asSamplerType(sampler_type)
    current_index += 1

    use_primary_edge_sampling = args[current_index]
    current_index += 1
    use_secondary_edge_sampling = args[current_index]
    current_index += 1

    scene = redner.Scene(camera,
                            shapes,
                            materials,
                            area_lights,
                            envmap,
                            pyredner.get_use_gpu(),
                            -1, # pyredner.get_device().index if pyredner.get_device().index is not None else -1)
                            use_primary_edge_sampling,
                            use_secondary_edge_sampling
                            )

    # check that num_samples is a tuple
    if isinstance(num_samples, int):
        num_samples = (num_samples, num_samples)

    options = redner.RenderOptions(seed, 
                                    num_samples[0], 
                                    max_bounces, 
                                    channels,
                                    sampler_type)
    num_channels = redner.compute_num_channels(channels)

    rendered_image = tf.constant(np.zeros(
            shape=[resolution[0], resolution[1], num_channels], 
            dtype=np.float32), 
        dtype=tf.float32)

    start = time.time()

    # pdb.set_trace()
    redner.render(scene,
                options,
                redner.float_ptr(pyredner.data_ptr(rendered_image)),
                redner.float_ptr(0),
                None,
                redner.float_ptr(0))
    time_elapsed = time.time() - start
    if print_timing:
        print('Forward pass, time: %.5f s' % time_elapsed)

    # # For debugging
    # debug_img = tf.zeros((256, 256, 3), dtype=tf.float32)
    # redner.render(scene,
    #               options,
    #               redner.float_ptr(pyredner.data_ptr(rendered_image)),
    #               redner.float_ptr(0),
    #               None,
    #               redner.float_ptr(pyredner.data_ptr(debug_img)))
    # pyredner.imwrite(debug_img, 'debug.png')
    # exit()

    # import pdb; pdb.set_trace()

    ctx.shapes = shapes
    ctx.materials = materials
    ctx.area_lights = area_lights
    ctx.envmap = envmap
    ctx.scene = scene
    ctx.options = options
    ctx.num_samples = num_samples
    ctx.num_channels = __num_channels
    return rendered_image

@tf.custom_gradient
def render(*x):
    seed, args = int(x[0]), x[1:]
    img = forward(seed, *args)

    def backward(grad_img):
        global __ctx
        ctx = __ctx
        scene = ctx.scene
        options = ctx.options
        d_position = tf.constant(np.zeros(3, dtype=np.float32), dtype=tf.float32, name="d_position")
        d_look_at = tf.constant(np.zeros(3, dtype=np.float32), dtype=tf.float32, name="d_look_at")
        d_up = tf.constant(np.zeros(3, dtype=np.float32), dtype=tf.float32, name="d_up")
        d_ndc_to_cam = tf.constant(np.zeros([3,3], dtype=np.float32), dtype=tf.float32, name="d_ndc_to_cam")
        d_cam_to_ndc = tf.constant(np.zeros([3,3], dtype=np.float32), dtype=tf.float32, name="d_cam_to_ndc")
        d_camera = redner.DCamera(redner.float_ptr(pyredner.data_ptr(d_position)),
                                  redner.float_ptr(pyredner.data_ptr(d_look_at)),
                                  redner.float_ptr(pyredner.data_ptr(d_up)),
                                  redner.float_ptr(pyredner.data_ptr(d_ndc_to_cam)),
                                  redner.float_ptr(pyredner.data_ptr(d_cam_to_ndc)))
        d_vertices_list = []
        d_uvs_list = []
        d_normals_list = []
        d_shapes = []
        for i, shape in enumerate(ctx.shapes):
            num_vertices = shape.num_vertices
            d_vertices = tf.constant(np.zeros([num_vertices, 3],dtype=np.float32), dtype=tf.float32, name="d_vertices_{}".format(i))
            d_uvs = tf.constant(np.zeros([num_vertices, 2],dtype=np.float32), dtype=tf.float32, name="d_uvs_{}".format(i)) if shape.has_uvs() else None
            d_normals = tf.constant(np.zeros([num_vertices, 3],dtype=np.float32), dtype=tf.float32, name="d_normals_{}".format(i)) if shape.has_normals() else None
            d_vertices_list.append(d_vertices)
            d_uvs_list.append(d_uvs)
            d_normals_list.append(d_normals)
            d_shapes.append(redner.DShape(\
                redner.float_ptr(pyredner.data_ptr(d_vertices)),
                redner.float_ptr(pyredner.data_ptr(d_uvs) if d_uvs is not None else 0),
                redner.float_ptr(pyredner.data_ptr(d_normals) if d_normals is not None else 0)))

        d_diffuse_list = []
        d_specular_list = []
        d_roughness_list = []
        d_materials = []
        for material in ctx.materials:
            diffuse_size = material.get_diffuse_size()
            specular_size = material.get_specular_size()
            roughness_size = material.get_roughness_size()
            if diffuse_size[0] == 0:
                d_diffuse = tf.convert_to_tensor(np.zeros(3, dtype=np.float32), dtype=tf.float32)
            else:
                d_diffuse = tf.convert_to_tensor(np.zeros([diffuse_size[2],
                                        diffuse_size[1],
                                        diffuse_size[0],
                                        3], dtype=np.float32), dtype=tf.float32)
            if specular_size[0] == 0:
                d_specular = tf.convert_to_tensor(np.zeros([3], dtype=np.float32), dtype=tf.float32)
            else:
                d_specular = tf.convert_to_tensor(np.zeros([specular_size[2],
                                         specular_size[1],
                                         specular_size[0],
                                         3], dtype=np.float32), dtype=tf.float32)
            if roughness_size[0] == 0:
                d_roughness = tf.convert_to_tensor(np.zeros([1], dtype=np.float32), dtype=tf.float32)
            else:
                d_roughness = tf.convert_to_tensor(np.zeros([roughness_size[2],
                                          roughness_size[1],
                                          roughness_size[0],
                                          1], dtype=np.float32), dtype=tf.float32)
            d_diffuse_list.append(d_diffuse)
            d_specular_list.append(d_specular)
            d_roughness_list.append(d_roughness)
            d_diffuse_uv_scale = tf.convert_to_tensor(np.zeros([2], dtype=np.float32), dtype=tf.float32)
            d_specular_uv_scale = tf.convert_to_tensor(np.zeros([2], dtype=np.float32), dtype=tf.float32)
            d_roughness_uv_scale = tf.convert_to_tensor(np.zeros([2], dtype=np.float32), dtype=tf.float32)
            d_diffuse_tex = redner.Texture3(\
                redner.float_ptr(pyredner.data_ptr(d_diffuse)),
                diffuse_size[0], diffuse_size[1], diffuse_size[2],
                redner.float_ptr(pyredner.data_ptr(d_diffuse_uv_scale)))
            d_specular_tex = redner.Texture3(\
                redner.float_ptr(pyredner.data_ptr(d_specular)),
                specular_size[0], specular_size[1], specular_size[2],
                redner.float_ptr(pyredner.data_ptr(d_specular_uv_scale)))
            d_roughness_tex = redner.Texture1(\
                redner.float_ptr(pyredner.data_ptr(d_roughness)),
                roughness_size[0], roughness_size[1], roughness_size[2],
                redner.float_ptr(pyredner.data_ptr(d_roughness_uv_scale)))
            d_materials.append(redner.DMaterial(\
                d_diffuse_tex, d_specular_tex, d_roughness_tex))

        d_intensity_list = []
        d_area_lights = []
        for light in ctx.area_lights:
            d_intensity = tf.convert_to_tensor(np.zeros([3], dtype=np.float32), dtype=tf.float32)
            d_intensity_list.append(d_intensity)
            d_area_lights.append(\
                redner.DAreaLight(redner.float_ptr(pyredner.data_ptr(d_intensity))))

        d_envmap = None
        if ctx.envmap is not None:
            print(">>> Get d_envmap")
            envmap = ctx.envmap
            size = envmap.get_size()
            d_envmap_values = \
                tf.convert_to_tensor(np.zeros([size[2],
                            size[1],
                            size[0],
                            3], dtype=np.float32
                        ), dtype=tf.float32)
            d_envmap_uv_scale = tf.convert_to_tensor(np.zeros([2], dtype=np.float32), dtype=tf.float32)
            d_envmap_tex = redner.Texture3(\
                redner.float_ptr(pyredner.data_ptr(d_envmap_values)),
                size[0], size[1], size[2],
                redner.float_ptr(pyredner.data_ptr(d_envmap_uv_scale)))
            d_world_to_env = tf.convert_to_tensor(np.zeros([4, 4], dtype=np.float32), dtype=tf.float32)
            d_envmap = redner.DEnvironmentMap(\
                d_envmap_tex,
                redner.float_ptr(pyredner.data_ptr(d_world_to_env)))

        d_scene = redner.DScene(d_camera,
                                d_shapes,
                                d_materials,
                                d_area_lights,
                                d_envmap,
                                pyredner.get_use_gpu(),
                                -1)
        if not get_use_correlated_random_number():
            # Decod_uple the forward/backward random numbers by adding a big prime number
            options.seed += 1000003
        start = time.time()
        # pdb.set_trace()

        options.num_samples = ctx.num_samples[1]
        redner.render(scene,  
                      options,
                      redner.float_ptr(0),    # rendered_image
                      redner.float_ptr(pyredner.data_ptr(grad_img)),
                      d_scene,
                      redner.float_ptr(0))    # debug_image
        time_elapsed = time.time() - start
        
        if print_timing:
            print('Backward pass, time: %.5f s' % time_elapsed)

        # # For debugging
        # pyredner.imwrite(grad_img, 'grad_img.exr')
        # grad_img = tf.ones([256, 256, 3], dtype=tf.float32)
        # debug_img = tf.zeros([256, 256, 3], dtype=tf.float32)
        # redner.render(scene, options,
        #               redner.float_ptr(0),
        #               redner.float_ptr(pyredner.data_ptr(grad_img)),
        #               d_scene,
        #               redner.float_ptr(pyredner.data_ptr(debug_img)))
        # pyredner.imwrite(debug_img, 'debug.exr')
        # pyredner.imwrite(-debug_img, 'debug_.exr')
        # exit()

        ret_list = []
        ret_list.append(None) # seed 0
        ret_list.append(None) # num_shapes 1
        ret_list.append(None) # num_materials 2 
        ret_list.append(None) # num_lights 3
        ret_list.append(d_position)  # 4
        ret_list.append(d_look_at)      # 5
        ret_list.append(d_up)        # 6
        ret_list.append(d_ndc_to_cam)    # 7
        ret_list.append(d_cam_to_ndc)    # 8
        ret_list.append(None) # clip near  9
        ret_list.append(None) # resolution 10
        ret_list.append(None) # camera_type 11

        num_shapes = len(ctx.shapes)
        for i in range(num_shapes):  # 1
            ret_list.append(d_vertices_list[i])  #12
            ret_list.append(None) # indices      #13
            ret_list.append(d_uvs_list[i])       #14
            ret_list.append(d_normals_list[i])   #15
            ret_list.append(None) # material id  #16
            ret_list.append(None) # light id     #17

        num_materials = len(ctx.materials) # 1
        for i in range(num_materials):
            ret_list.append(d_diffuse_list[i])   #18
            ret_list.append(None) # diffuse_uv_scale #19
            ret_list.append(d_specular_list[i])      #20
            ret_list.append(None) # specular_uv_scale 21
            ret_list.append(d_roughness_list[i])      #22
            ret_list.append(None) # roughness_uv_scale 23
            ret_list.append(None) # two sided         24

        num_area_lights = len(ctx.area_lights)
        for i in range(num_area_lights):
            ret_list.append(None) # shape id          
            ret_list.append(d_intensity_list[i].cpu()) #
            ret_list.append(None) # two sided         

        if ctx.envmap is not None:
            ret_list.append(d_envmap_values)                   #25
            ret_list.append(None) # uv_scale          26
            ret_list.append(None) # env_to_world      27
            ret_list.append(d_world_to_env)                    #28
            ret_list.append(None) # sample_cdf_ys     29
            ret_list.append(None) # sample_cdf_xs     30
            ret_list.append(None) # pdf_norm          31
        else:
            ret_list.append(None)
            ret_list.append(None)
            ret_list.append(None)
            ret_list.append(None)
            ret_list.append(None)
            ret_list.append(None)
            ret_list.append(None)
        
        ret_list.append(None) # num samples
        ret_list.append(None) # num bounces
        ret_list.append(None) # num channels
        for _ in range(ctx.num_channels):
            ret_list.append(None) # channel

        ret_list.append(None) # sampler type
        ret_list.append(None) # use_primary_edge_sampling
        ret_list.append(None) # use_secondary_edge_sampling

        '''
        For test_envmap.py, len(ret_list) = 39.
            for i in range(len(ret_list)): print(ret_list[i].shape, i)
                (0,) 0                                                 
                (0,) 1
                (0,) 2
                (0,) 3
                (3,) 4
                (3,) 5
                (3,) 6
                (3, 3) 7
                (3, 3) 8
                (0,) 9
                (0,) 10
                (0,) 11
                (8192, 3) 12
                (0,) 13
                (8192, 2) 14
                (8192, 3) 15
                (0,) 16
                (0,) 17
                (3,) 18
                (0,) 19
                (3,) 20
                (0,) 21
                (1,) 22
                (0,) 23
                (0,) 24
                (7, 32, 64, 3) 25
                (0,) 26
                (0,) 27
                (4, 4) 28
                (0,) 29
                (0,) 30
                (0,) 31
                (0,) 32
                (0,) 33
                (0,) 34
                (0,) 35
                (0,) 36
                (0,) 37
                (0,) 38
        '''
        # pdb.set_trace()
        return ret_list

    return img, backward