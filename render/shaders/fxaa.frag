#version 330
// fxaa.frag - Fast Approximate Anti-Aliasing (after Lottes' FXAA 3.11,
// "quality" preset, simplified).
//
// Works purely on the final LDR image: find pixels on a luma edge, estimate
// the edge direction from the 3x3 neighbourhood, walk along the edge in both
// directions to find its ends, then blend with the neighbour across the edge
// by an amount that reconstructs a sub-pixel accurate edge position.

uniform sampler2D u_ldr;
uniform vec2 u_texel;          // 1 / resolution
uniform float u_enabled;

in vec2 v_uv;
layout(location = 0) out vec4 o_color;

#define EDGE_MIN 0.0312
#define EDGE_MAX 0.125
#define SUBPIX 0.75
#define STEPS 10

float lumaAt(vec2 uv) { return textureLod(u_ldr, uv, 0.0).a; }

void main() {
    vec4 center = textureLod(u_ldr, v_uv, 0.0);
    if (u_enabled < 0.5) { o_color = vec4(center.rgb, 1.0); return; }
    float lM = center.a;
    float lN = lumaAt(v_uv + vec2(0.0, u_texel.y));
    float lS = lumaAt(v_uv - vec2(0.0, u_texel.y));
    float lE = lumaAt(v_uv + vec2(u_texel.x, 0.0));
    float lW = lumaAt(v_uv - vec2(u_texel.x, 0.0));
    float lMax = max(lM, max(max(lN, lS), max(lE, lW)));
    float lMin = min(lM, min(min(lN, lS), min(lE, lW)));
    float range = lMax - lMin;
    if (range < max(EDGE_MIN, lMax * EDGE_MAX)) { o_color = vec4(center.rgb, 1.0); return; }

    float lNE = lumaAt(v_uv + u_texel);
    float lNW = lumaAt(v_uv + vec2(-u_texel.x, u_texel.y));
    float lSE = lumaAt(v_uv + vec2(u_texel.x, -u_texel.y));
    float lSW = lumaAt(v_uv - u_texel);

    // sub-pixel aliasing amount
    float lAvg = (2.0 * (lN + lS + lE + lW) + lNE + lNW + lSE + lSW) / 12.0;
    float sub = clamp(abs(lAvg - lM) / range, 0.0, 1.0);
    sub = smoothstep(0.0, 1.0, sub);
    sub = sub * sub * SUBPIX;

    // horizontal or vertical edge?
    float edgeH = abs(lNW + lNE - 2.0 * lN) + 2.0 * abs(lW + lE - 2.0 * lM) + abs(lSW + lSE - 2.0 * lS);
    float edgeV = abs(lNW + lSW - 2.0 * lW) + 2.0 * abs(lN + lS - 2.0 * lM) + abs(lNE + lSE - 2.0 * lE);
    bool horizontal = edgeH >= edgeV;

    float l1 = horizontal ? lS : lW;
    float l2 = horizontal ? lN : lE;
    float g1 = abs(l1 - lM);
    float g2 = abs(l2 - lM);
    bool steepest1 = g1 >= g2;
    float gradScaled = 0.25 * max(g1, g2);
    float stepLen = horizontal ? u_texel.y : u_texel.x;
    float lLocal;
    if (steepest1) { stepLen = -stepLen; lLocal = 0.5 * (l1 + lM); }
    else { lLocal = 0.5 * (l2 + lM); }

    vec2 cur = v_uv;
    if (horizontal) cur.y += stepLen * 0.5; else cur.x += stepLen * 0.5;
    vec2 off = horizontal ? vec2(u_texel.x, 0.0) : vec2(0.0, u_texel.y);

    vec2 uv1 = cur - off;
    vec2 uv2 = cur + off;
    float e1 = lumaAt(uv1) - lLocal;
    float e2 = lumaAt(uv2) - lLocal;
    bool done1 = abs(e1) >= gradScaled;
    bool done2 = abs(e2) >= gradScaled;
    for (int i = 0; i < STEPS && !(done1 && done2); ++i) {
        float s = (i < 3) ? 1.0 : 2.0;
        if (!done1) { uv1 -= off * s; e1 = lumaAt(uv1) - lLocal; done1 = abs(e1) >= gradScaled; }
        if (!done2) { uv2 += off * s; e2 = lumaAt(uv2) - lLocal; done2 = abs(e2) >= gradScaled; }
    }
    float d1 = horizontal ? (v_uv.x - uv1.x) : (v_uv.y - uv1.y);
    float d2 = horizontal ? (uv2.x - v_uv.x) : (uv2.y - v_uv.y);
    bool dir1 = d1 < d2;
    float dmin = min(d1, d2);
    float span = d1 + d2;
    float pixelOffset = -dmin / span + 0.5;
    bool lMSmaller = lM < lLocal;
    bool correct = ((dir1 ? e1 : e2) < 0.0) != lMSmaller;
    float finalOffset = correct ? pixelOffset : 0.0;
    finalOffset = max(finalOffset, sub);

    vec2 fuv = v_uv;
    if (horizontal) fuv.y += finalOffset * stepLen; else fuv.x += finalOffset * stepLen;
    o_color = vec4(textureLod(u_ldr, fuv, 0.0).rgb, 1.0);
}
