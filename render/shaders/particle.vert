#version 330
// particle.vert - expands each particle (4 identical vertices + a corner
// coordinate) into a quad in world space.
//
//  * normal particles: camera-facing quad, rotated by params.y
//  * stretched particles (params.z > 0): the quad's long axis follows the
//    velocity projected onto the screen plane and is lengthened by
//    |velocity| * params.z (a cheap motion-blur streak for sparks/tracers)
//
// Alpha-blended particles (smoke/dust) receive approximate lighting: sky
// irradiance from the SH coefficients plus a share of the sun, both scaled
// by the baked sky-visibility volume so smoke inside buildings is darker.

uniform mat4 p3d_ModelViewProjectionMatrix;
uniform vec3 u_camPos;
uniform vec3 u_sunColor;
uniform vec3 u_sh[9];
uniform sampler3D u_skyVis;
uniform vec3 u_skyVisMin;
uniform vec3 u_skyVisInvSize;
uniform vec3 u_ambientFloor;

in vec4 p3d_Vertex;          // particle centre (world space)
in vec2 p3d_MultiTexCoord0;  // corner (0..1)
in vec4 p3d_Color;
in vec4 params;              // size, rotation, stretch, atlas frame
in vec4 velocity;            // xyz velocity, w = lighting amount

out vec2 v_uv;
out vec4 v_color;
out vec3 v_light;

vec3 sh(vec3 n) {
    return max(u_sh[0] + u_sh[1] * n.y + u_sh[2] * n.z + u_sh[3] * n.x
        + u_sh[4] * (n.x * n.y) + u_sh[5] * (n.y * n.z) + u_sh[6] * (3.0 * n.z * n.z - 1.0)
        + u_sh[7] * (n.x * n.z) + u_sh[8] * (n.x * n.x - n.y * n.y), vec3(0.0));
}

void main() {
    vec3 c = p3d_Vertex.xyz;
    vec3 fwd = normalize(c - u_camPos);
    vec3 worldUp = abs(fwd.z) > 0.98 ? vec3(0.0, 1.0, 0.0) : vec3(0.0, 0.0, 1.0);
    vec3 right = normalize(cross(fwd, worldUp));
    vec3 up = cross(right, fwd);
    vec2 corner = p3d_MultiTexCoord0 * 2.0 - 1.0;
    float size = params.x;
    vec3 offset;
    if (params.z > 0.0) {
        vec3 vel = velocity.xyz;
        vec3 vp = vel - fwd * dot(vel, fwd);
        float len = length(vp);
        vec3 axis = len > 1e-4 ? vp / len : up;
        vec3 side = normalize(cross(fwd, axis));
        offset = side * corner.x * size + axis * corner.y * (size + params.z * len);
    } else {
        float s = sin(params.y);
        float co = cos(params.y);
        vec2 rc = vec2(corner.x * co - corner.y * s, corner.x * s + corner.y * co);
        offset = (right * rc.x + up * rc.y) * size;
    }
    gl_Position = p3d_ModelViewProjectionMatrix * vec4(c + offset, 1.0);

    float f = params.w;
    vec2 cell = vec2(mod(f, 4.0), floor(f / 4.0));
    v_uv = (cell + clamp(p3d_MultiTexCoord0, 0.01, 0.99)) * 0.25;
    v_color = p3d_Color;

    float lit = velocity.w;
    if (lit > 0.0) {
        float vis = texture(u_skyVis, (c - u_skyVisMin) * u_skyVisInvSize).a;
        vec3 amb = sh(vec3(0.0, 0.0, 1.0)) * 0.55 + sh(-fwd) * 0.45;
        vec3 light = (amb * 1.1 + u_sunColor * 0.12) * vis + u_ambientFloor * 4.0 + vec3(0.02);
        v_light = mix(vec3(1.0), light, lit);
    } else {
        v_light = vec3(1.0);
    }
}
