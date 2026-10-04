#version 330
// prepass.frag - store the geometric world-space normal (0..1 encoded).
// Geometric (not normal-mapped) normals are what AO wants: AO describes
// large-scale occlusion; fine detail comes from the material AO maps.

in vec3 v_normal;
layout(location = 0) out vec4 o_normal;

void main() {
    vec3 n = normalize(v_normal);
    if (!gl_FrontFacing) n = -n;
    o_normal = vec4(n * 0.5 + 0.5, 1.0);
}
