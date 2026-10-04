#version 330
// tonemap.frag - HDR scene -> display image (the "uber" post pass).
//
// In HDR (linear, scene-referred) space:
//   1. chromatic aberration (optional): red and blue are sampled slightly
//      apart toward the screen edges, like a cheap lens,
//   2. bloom is added on top of the exposed image (it was thresholded and
//      exposed in the first downsample, so it adds only to bright areas),
//   3. exposure from eye adaptation (exposure.glsl),
//   4. vignette: natural lens falloff toward the corners,
//   5. grading: white balance (temperature / tint), saturation, and contrast
//      around mid-grey in log space (keeps black and white points stable),
// then:
//   6. ACES filmic tone mapping (Stephen Hill's fit of the RRT+ODT in
//      ACEScg) compresses highlights with a soft shoulder,
//   7. linear -> sRGB,
//   8. lift / gamma / gain in display space (shadows, mid-tones, highlights),
//   9. film grain (optional) and +/- half-LSB dither against 8-bit banding.
// The output stores luma in alpha for the FXAA pass that follows.

#include "common.glsl"
#include "exposure.glsl"

uniform sampler2D u_scene;
uniform sampler2D u_bloom;
uniform vec4 u_bloomMix;       // x: intensity
uniform vec4 u_grade;          // x: saturation, y: contrast, z: temperature (-1..1), w: tint (-1..1)
uniform vec4 u_lift;           // rgb (display space, additive in shadows)
uniform vec4 u_gamma;          // rgb (mid-tones, 1 = neutral)
uniform vec4 u_gain;           // rgb (multiplier)
uniform vec4 u_lensFx;         // x: vignette strength, y: vignette start radius, z: chromatic aberration, w: grain
uniform vec4 u_misc;           // x: dither strength, y: time (s), z: aspect ratio, w: unused

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

// Simple white balance: warm/cool along the blue-orange axis, tint along
// green-magenta, normalised so overall luminance is unchanged.
vec3 whiteBalance(vec3 c, float temperature, float tint) {
    vec3 m = vec3(1.0 + 0.10 * temperature, 1.0 - 0.06 * tint, 1.0 - 0.10 * temperature);
    return c * m / luminance(m);
}

float hash12(vec2 p) {
    vec3 p3 = fract(vec3(p.xyx) * 0.1031);
    p3 += dot(p3, p3.yzx + 33.33);
    return fract((p3.x + p3.y) * p3.z);
}

void main() {
    vec2 centered = v_uv - 0.5;
    vec3 hdr;
    if (u_lensFx.z > 0.0) {
        vec2 shift = centered * dot(centered, centered) * u_lensFx.z * 0.06;
        hdr.r = texture(u_scene, v_uv - shift).r;
        hdr.g = texture(u_scene, v_uv).g;
        hdr.b = texture(u_scene, v_uv + shift).b;
    } else {
        hdr = texture(u_scene, v_uv).rgb;
    }
    hdr = max(hdr, vec3(0.0));

    vec3 c = hdr * sceneExposure();
    c += texture(u_bloom, v_uv).rgb * u_bloomMix.x;

    // vignette (radius 0 at the centre, 1 at the corners)
    float r = length(centered * vec2(u_misc.z, 1.0)) / length(vec2(u_misc.z, 1.0) * 0.5);
    c *= mix(1.0, 1.0 - u_lensFx.x, smoothstep(u_lensFx.y, 1.15, r));

    // grading in linear space
    c = whiteBalance(c, u_grade.z, u_grade.w);
    float l = luminance(c);
    c = max(mix(vec3(l), c, u_grade.x), vec3(0.0));
    c = 0.18 * pow(c / 0.18 + 1e-6, vec3(u_grade.y));

    c = acesFitted(c);
    c = linearToSRGB(c);

    // lift / gamma / gain (ASC-CDL-like) in display space
    c = c * u_gain.rgb + u_lift.rgb * (1.0 - c);
    c = pow(max(c, vec3(0.0)), 1.0 / max(u_gamma.rgb, vec3(0.01)));

    if (u_lensFx.w > 0.0) {
        float g = hash12(gl_FragCoord.xy + fract(u_misc.y * 7.13) * 512.0) - 0.5;
        float lumD = dot(c, vec3(0.299, 0.587, 0.114));
        c += g * u_lensFx.w * (1.0 - lumD * 0.7);
    }

    float n1 = interleavedGradientNoise(gl_FragCoord.xy);
    float n2 = interleavedGradientNoise(gl_FragCoord.xy + vec2(47.0, 13.0));
    c += (n1 - n2) * u_misc.x / 255.0;
    c = saturate(c);
    o_color = vec4(c, dot(c, vec3(0.299, 0.587, 0.114)));
}
