#version 330
// luminance.frag - first step of eye adaptation: average log-luminance.
//
// Rendered into a small (64x64) target. Each output pixel averages 16
// bilinear taps of its cell of the HDR scene and stores
//   r = w * log2(luminance),  g = w
// where w is a centre weight (the eye adapts mostly to what you look at).
// Reduction passes average these down to 1x1; r/g is then the weighted
// mean log-luminance (a geometric mean, robust against tiny bright areas).

#include "common.glsl"

uniform sampler2D u_scene;
uniform vec2 u_cell;            // size of one output cell in uv

in vec2 v_uv;
layout(location = 0) out vec4 o_lum;

void main() {
    vec2 origin = v_uv - u_cell * 0.5;
    float sum = 0.0;
    for (int y = 0; y < 4; ++y) {
        for (int x = 0; x < 4; ++x) {
            vec2 uv = origin + (vec2(x, y) + 0.5) * 0.25 * u_cell;
            float l = luminance(textureLod(u_scene, uv, 0.0).rgb);
            sum += clamp(log2(max(l, 1e-5)), -12.0, 8.0);
        }
    }
    vec2 c = (v_uv - 0.5) * vec2(1.6, 1.0);
    float w = 1.0 + 2.0 * exp(-dot(c, c) * 6.0);
    o_lum = vec4(sum / 16.0 * w, w, 0.0, 1.0);
}
