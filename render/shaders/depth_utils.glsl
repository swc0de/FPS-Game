// depth_utils.glsl - reconstructing view-space data from a depth texture.
//
// Panda/OpenGL perspective depth is non-linear: d = 0.5 * ndc + 0.5 with
// ndc = (f + n - 2fn / z) / (f - n) for view distance z. Inverting it gives
// the linear distance along the view axis. View space here is the usual
// OpenGL convention: +x right, +y up, the camera looks down -z.

uniform vec4 u_proj;   // x: tan(hfov/2), y: tan(vfov/2), z: near, w: far

float linearDepth(float d) {
    float n = u_proj.z;
    float f = u_proj.w;
    float ndc = d * 2.0 - 1.0;
    return 2.0 * n * f / (f + n - ndc * (f - n));
}

// uv in [0,1] (origin bottom-left), z = linear distance along the view axis
vec3 viewPosition(vec2 uv, float z) {
    vec2 ndc = uv * 2.0 - 1.0;
    return vec3(ndc.x * u_proj.x * z, ndc.y * u_proj.y * z, -z);
}
