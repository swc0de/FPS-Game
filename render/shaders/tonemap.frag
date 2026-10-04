#version 330
// tonemap.frag - HDR -> display.
//
// 1. Exposure scales scene radiance.
// 2. ACES filmic tone mapping (Stephen Hill's fit of the RRT+ODT, applied in
//    ACEScg via the input/output matrices) compresses highlights with a
//    pleasing shoulder and slight hue shift of very bright colours.
// 3. Linear -> sRGB transfer function.
// 4. +/- half-LSB triangular dither removes 8-bit banding in skies/fog.
// The output stores luma in alpha for the FXAA pass that follows.

#include "common.glsl"

uniform sampler2D u_scene;
uniform vec4 u_tonemapParams;  // x: exposure, y: dither strength, z/w unused

in vec2 v_uv;
layout(location = 0) out vec4 o_color;

const mat3 ACESInput = mat3(
    0.59719, 0.07600, 0.02840,
    0.35458, 0.90834, 0.13383,
    0.04823, 0.01566, 0.83777);
const mat3 ACESOutput = mat3(
     1.60475, -0.10208, -0.00327,
    -0.53108,  1.10813, -0.07276,
    -0.07367, -0.00605,  1.07602);

vec3 RRTAndODTFit(vec3 v) {
    vec3 a = v * (v + 0.0245786) - 0.000090537;
    vec3 b = v * (0.983729 * v + 0.4329510) + 0.238081;
    return a / b;
}

vec3 acesFitted(vec3 c) {
    c = ACESInput * c;
    c = RRTAndODTFit(c);
    c = ACESOutput * c;
    return saturate(c);
}

vec3 linearToSRGB(vec3 c) {
    vec3 lo = c * 12.92;
    vec3 hi = 1.055 * pow(c, vec3(1.0 / 2.4)) - 0.055;
    return mix(lo, hi, step(vec3(0.0031308), c));
}

void main() {
    vec3 hdr = texture(u_scene, v_uv).rgb;
    hdr = max(hdr, vec3(0.0));
    vec3 c = acesFitted(hdr * u_tonemapParams.x);
    c = linearToSRGB(c);
    float n1 = interleavedGradientNoise(gl_FragCoord.xy);
    float n2 = interleavedGradientNoise(gl_FragCoord.xy + vec2(47.0, 13.0));
    c += (n1 - n2) * u_tonemapParams.y / 255.0;
    o_color = vec4(c, dot(c, vec3(0.299, 0.587, 0.114)));
}
