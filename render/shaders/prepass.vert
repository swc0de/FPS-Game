#version 330
// prepass.vert - depth + normal pre-pass ("thin G-buffer").
//
// The world is drawn once more before the main pass with this trivial shader
// into a depth texture and an RGBA8 world-space normal target. Screen-space
// passes that must run *before* the forward lighting pass read them:
//   * GTAO (ambient occlusion) - so AO can darken only the ambient term,
//   * soft particles - smoke/dust fade where they intersect geometry.
// Transparent things (sky, particles, decals, text, viewmodel) are hidden
// from the pre-pass camera.

uniform mat4 p3d_ModelViewProjectionMatrix;
uniform mat4 p3d_ModelMatrix;

in vec4 p3d_Vertex;
in vec3 p3d_Normal;

out vec3 v_normal;

#include "skinning.glsl"

void main() {
    mat4 b = skin_matrix();
    gl_Position = p3d_ModelViewProjectionMatrix * (b * p3d_Vertex);
    v_normal = mat3(p3d_ModelMatrix) * (mat3(b) * p3d_Normal);
}
