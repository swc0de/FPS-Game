#version 330
// pbr.vert - transforms geometry and passes a world-space tangent frame
// to the fragment shader for normal mapping.

uniform mat4 p3d_ModelViewProjectionMatrix;
uniform mat4 p3d_ModelMatrix;

in vec4 p3d_Vertex;
in vec3 p3d_Normal;
in vec3 p3d_Tangent;
in vec3 p3d_Binormal;
in vec2 p3d_MultiTexCoord0;

#include "skinning.glsl"

out vec3 v_worldPos;
out vec3 v_normal;
out vec3 v_tangent;
out vec3 v_binormal;
out vec2 v_uv;

void main() {
    mat4 b = skin_matrix();
    vec4 vtx = b * p3d_Vertex;
    vec4 wp = p3d_ModelMatrix * vtx;
    v_worldPos = wp.xyz;
    // models are only uniformly scaled, so the upper 3x3 is fine for directions
    mat3 m = mat3(p3d_ModelMatrix) * mat3(b);
    v_normal = m * p3d_Normal;
    v_tangent = m * p3d_Tangent;
    v_binormal = m * p3d_Binormal;
    v_uv = p3d_MultiTexCoord0;
    gl_Position = p3d_ModelViewProjectionMatrix * vtx;
}
