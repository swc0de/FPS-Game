#version 330
// character.vert - soldier bodies (Milestone 9).
//
// The mesh is stored in the skeleton's bind pose and moved by linear blend
// skinning (skinning.glsl). Besides the posed world position and normal, the
// fragment shader gets the bind-pose position and normal: detail textures
// (skin pores, fabric weave, wear) are projected in bind space, so they stick
// to the body however it moves, with no UV layout to author. The matrix that
// takes bind-space directions to world space comes along for the detail
// normals.

uniform mat4 p3d_ModelViewProjectionMatrix;
uniform mat4 p3d_ModelMatrix;

in vec4 p3d_Vertex;
in vec3 p3d_Normal;
in vec4 p3d_Color;
in float char_slot;

#include "skinning.glsl"

out vec3 v_worldPos;
out vec3 v_normal;
out vec3 v_bindPos;
out vec3 v_bindNormal;
out mat3 v_bindToWorld;
out vec4 v_color;
flat out int v_slot;

void main() {
    mat4 b = skin_matrix();
    vec4 vtx = b * p3d_Vertex;
    v_worldPos = (p3d_ModelMatrix * vtx).xyz;
    v_bindToWorld = mat3(p3d_ModelMatrix) * mat3(b);
    v_normal = v_bindToWorld * p3d_Normal;
    v_bindPos = p3d_Vertex.xyz;
    v_bindNormal = p3d_Normal;
    v_color = p3d_Color;
    v_slot = int(char_slot + 0.5);
    gl_Position = p3d_ModelViewProjectionMatrix * vtx;
}
