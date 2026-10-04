#version 330
// sky.vert - a unit sphere that follows the camera. Depth is forced to the
// far plane (z = w) so the sky is behind everything without depth writes.
uniform mat4 p3d_ModelViewProjectionMatrix;
uniform mat4 p3d_ModelMatrix;
uniform vec3 u_camPos;
in vec4 p3d_Vertex;
out vec3 v_dir;
void main() {
    vec4 wp = p3d_ModelMatrix * p3d_Vertex;
    v_dir = wp.xyz - u_camPos;
    vec4 clip = p3d_ModelViewProjectionMatrix * p3d_Vertex;
    gl_Position = clip.xyww;
}
