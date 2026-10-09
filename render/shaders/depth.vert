#version 330
// depth.vert - shadow caster pass: position only.
uniform mat4 p3d_ModelViewProjectionMatrix;
in vec4 p3d_Vertex;
#include "skinning.glsl"
void main() {
    gl_Position = p3d_ModelViewProjectionMatrix * (skin_matrix() * p3d_Vertex);
}
