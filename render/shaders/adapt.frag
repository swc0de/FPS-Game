#version 330
// adapt.frag - temporal eye adaptation (1x1 target, ping-pong between two
// buffers so this frame can read last frame's value).
//
// The adapted value moves exponentially toward this frame's average
// log-luminance. Adapting to brightness (stepping outside) is faster than
// adapting to darkness, like the human eye.

uniform sampler2D u_avg;        // 1x1: r = weighted log-lum sum, g = weight
uniform sampler2D u_prev;       // last frame's adapted value (r)
uniform vec4 u_adaptRates;      // x: rate when brightening, y: rate when darkening, z: 1 = reset

layout(location = 0) out vec4 o_value;

void main() {
    vec4 a = texelFetch(u_avg, ivec2(0, 0), 0);
    float target = a.r / max(a.g, 1e-4);
    float prev = texelFetch(u_prev, ivec2(0, 0), 0).r;
    float rate = target > prev ? u_adaptRates.x : u_adaptRates.y;
    float v = u_adaptRates.z > 0.5 ? target : mix(prev, target, rate);
    o_value = vec4(v, target, 0.0, 1.0);
}
