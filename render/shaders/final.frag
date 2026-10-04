#version 330
// final.frag - last pass, drawn into the window.
//
// * Upscales the anti-aliased image from the render resolution (render
//   scale < 100%) to the window with bilinear filtering.
// * Contrast Adaptive Sharpening (after AMD FidelityFX CAS): a 5-tap
//   sharpening filter whose strength per pixel depends on local contrast,
//   so flat areas get crisper without halos on already-sharp edges. It
//   recovers detail lost to FXAA or to a lower render scale.
// * Debug views (F3): ambient occlusion, bloom, normals, linear depth.

#include "common.glsl"
#include "depth_utils.glsl"

uniform sampler2D u_src;
uniform vec4 u_srcSize;         // xy: render size (px), zw: 1 / size
uniform vec4 u_finalParams;     // x: sharpness 0..1, y: debug view
uniform sampler2D u_dbgAO;
uniform sampler2D u_dbgBloom;
uniform sampler2D u_dbgNormal;
uniform sampler2D u_dbgDepth;

in vec2 v_uv;
layout(location = 0) out vec4 o_color;

vec3 casSharpen(vec2 uv, float sharpness) {
    vec2 t = u_srcSize.zw;
    vec3 b = textureLod(u_src, uv + vec2(0.0, t.y), 0.0).rgb;
    vec3 d = textureLod(u_src, uv - vec2(t.x, 0.0), 0.0).rgb;
    vec3 e = textureLod(u_src, uv, 0.0).rgb;
    vec3 f = textureLod(u_src, uv + vec2(t.x, 0.0), 0.0).rgb;
    vec3 h = textureLod(u_src, uv - vec2(0.0, t.y), 0.0).rgb;
    vec3 mn = min(min(min(d, e), min(f, b)), h);
    vec3 mx = max(max(max(d, e), max(f, b)), h);
    // amount of headroom before clipping, relative to the local maximum
    vec3 amp = sqrt(saturate(min(mn, 2.0 - mx) / max(mx, vec3(1e-4))));
    vec3 w = amp * (-1.0 / mix(8.0, 5.0, sharpness));
    return saturate((b * w + d * w + f * w + h * w + e) / (1.0 + 4.0 * w));
}

void main() {
    int mode = int(u_finalParams.y + 0.5);
    vec3 c;
    if (mode == 1) {
        c = vec3(texture(u_dbgAO, v_uv).r);
    } else if (mode == 2) {
        vec3 b = texture(u_dbgBloom, v_uv).rgb;
        c = b / (1.0 + b);
    } else if (mode == 3) {
        c = texture(u_dbgNormal, v_uv).rgb;
    } else if (mode == 4) {
        float z = linearDepth(texture(u_dbgDepth, v_uv).r);
        c = vec3(fract(log2(z + 1.0) * 0.5), saturate(z / 120.0), 1.0 - saturate(z / 30.0));
    } else if (u_finalParams.x > 0.001) {
        c = casSharpen(v_uv, u_finalParams.x);
    } else {
        c = textureLod(u_src, v_uv, 0.0).rgb;
    }
    o_color = vec4(c, 1.0);
}
