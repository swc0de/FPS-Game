#version 330
// gtao.frag - Ground Truth Ambient Occlusion (Jimenez et al. 2016), after
// the structure of Intel's XeGTAO reference, without temporal accumulation.
//
// For each pixel the hemisphere above the surface is cut into SLICES planes
// that contain the view vector. In every slice we march STEPS samples to
// both sides in screen space and keep the highest "horizon" (largest cosine
// between the view vector and the vector to the sample). The visible arc
// between the two horizons, clamped to the normal's hemisphere and weighted
// by the cosine (Lambert) term, has a closed-form integral:
//
//     a(h) = (cos n + 2 h sin n - cos(2h - n)) / 4
//
// where n is the angle of the normal projected into the slice. Averaging
// the slices gives the cosine-weighted visibility: 1 = open, 0 = occluded.
// The slice direction and step offsets are rotated per pixel with
// interleaved gradient noise; the following bilateral blur turns that
// noise into a smooth result.

#include "common.glsl"
#include "depth_utils.glsl"

uniform sampler2D u_depth;      // pre-pass depth
uniform sampler2D u_normal;     // pre-pass world normals (encoded 0..1)
uniform vec3 u_viewRight;       // camera basis in world space
uniform vec3 u_viewUp;
uniform vec3 u_viewBack;
uniform vec4 u_aoParams;        // x: radius (m), y: falloff start (fraction of radius), z: power, w: max radius (px)
uniform vec4 u_aoTarget;        // xy: depth texture size (px), zw: 1 / size
uniform vec2 u_aoFade;          // x: fade start distance (m), y: 1 / fade length

in vec2 v_uv;
layout(location = 0) out vec4 o_ao;

#ifndef AO_SLICES
#define AO_SLICES 3
#endif
#ifndef AO_STEPS
#define AO_STEPS 6
#endif

// Positions are always rebuilt at depth texel centres with exact fetches;
// mixing a pixel's uv with a neighbouring texel's depth (which happens when
// AO runs at half resolution) puts points slightly below sloped floors and
// shows up as stripes of false occlusion.
vec3 viewPosAt(ivec2 px) {
    px = clamp(px, ivec2(0), ivec2(u_aoTarget.xy) - 1);
    float d = texelFetch(u_depth, px, 0).r;
    return viewPosition((vec2(px) + 0.5) * u_aoTarget.zw, linearDepth(d));
}

void main() {
    ivec2 pix = ivec2(v_uv * u_aoTarget.xy);
    vec2 uv = (vec2(pix) + 0.5) * u_aoTarget.zw;
    float d = texelFetch(u_depth, pix, 0).r;
    if (d >= 0.999999) {               // sky
        o_ao = vec4(1.0, 1.0e4, 0.0, 1.0);
        return;
    }
    float z = linearDepth(d);
    vec3 P = viewPosition(uv, z);
    vec3 Nw = texelFetch(u_normal, pix, 0).xyz * 2.0 - 1.0;
    vec3 N = normalize(vec3(dot(Nw, u_viewRight), dot(Nw, u_viewUp), dot(Nw, u_viewBack)));
    vec3 V = normalize(-P);

    float radius = u_aoParams.x;
    // world radius projected to pixels at this depth
    float radiusPx = radius / (z * u_proj.y) * u_aoTarget.y * 0.5;
    radiusPx = min(radiusPx, u_aoParams.w);
    float fade = saturate((z - u_aoFade.x) * u_aoFade.y);
    if (radiusPx < 2.0 || fade >= 1.0) {
        o_ao = vec4(1.0, z, 0.0, 1.0);
        return;
    }

    float noiseSlice = interleavedGradientNoise(gl_FragCoord.xy);
    float noiseStep = interleavedGradientNoise(gl_FragCoord.xy + vec2(5.0, 37.0) * 7.31);
    float falloffRange = max(radius * (1.0 - u_aoParams.y), 1e-3);

    float visibility = 0.0;
    for (int slice = 0; slice < AO_SLICES; ++slice) {
        float phi = (float(slice) + noiseSlice) * PI / float(AO_SLICES);
        vec2 omega = vec2(cos(phi), sin(phi));          // screen direction (+x right, +y up)
        vec3 dirV = vec3(omega, 0.0);                    // the same direction in view space
        vec3 orthoDir = dirV - dot(dirV, V) * V;
        vec3 axis = normalize(cross(orthoDir, V));       // normal of the slice plane
        vec3 projN = N - axis * dot(N, axis);            // normal projected into the slice
        float projLen = length(projN);
        float signN = sign(dot(orthoDir, projN));
        float cosN = saturate(dot(projN, V) / max(projLen, 1e-4));
        float n = signN * acos(cosN);                    // angle of the projected normal

        // lowest possible horizons = the tangent plane on each side
        float lowCos0 = cos(n + 0.5 * PI);
        float lowCos1 = cos(n - 0.5 * PI);
        float hCos0 = lowCos0;
        float hCos1 = lowCos1;

        for (int k = 0; k < AO_STEPS; ++k) {
            float t = (float(k) + noiseStep) / float(AO_STEPS);
            t *= t;                                       // denser near the centre
            ivec2 off = ivec2(round(omega * (t * radiusPx + 1.3)));

            vec3 D0 = viewPosAt(pix + off) - P;
            vec3 D1 = viewPosAt(pix - off) - P;
            float l0 = length(D0);
            float l1 = length(D1);
            float c0 = dot(D0, V) / max(l0, 1e-4);
            float c1 = dot(D1, V) / max(l1, 1e-4);
            // distant samples fade back to the low horizon (no occlusion)
            float w0 = saturate((radius - l0) / falloffRange);
            float w1 = saturate((radius - l1) / falloffRange);
            hCos0 = max(hCos0, mix(lowCos0, c0, w0));
            hCos1 = max(hCos1, mix(lowCos1, c1, w1));
        }

        // horizon angles, clamped to the hemisphere around the normal
        float h0 = -acos(clamp(hCos1, -1.0, 1.0));
        float h1 = acos(clamp(hCos0, -1.0, 1.0));
        h0 = n + max(h0 - n, -0.5 * PI);
        h1 = n + min(h1 - n, 0.5 * PI);

        float sinN = sin(n);
        float arc0 = (cosN + 2.0 * h0 * sinN - cos(2.0 * h0 - n)) * 0.25;
        float arc1 = (cosN + 2.0 * h1 * sinN - cos(2.0 * h1 - n)) * 0.25;
        visibility += mix(projLen, 1.0, 0.05) * (arc0 + arc1);
    }
    visibility = saturate(visibility / float(AO_SLICES));
    visibility = pow(visibility, u_aoParams.z);
    visibility = mix(visibility, 1.0, fade);
    o_ao = vec4(visibility, z, 0.0, 1.0);
}
