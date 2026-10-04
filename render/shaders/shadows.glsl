// shadows.glsl - cascaded sun shadows and atlas-based local light shadows.
//
// CASCADED SHADOW MAPS
// --------------------
// The view frustum is split into CSM_CASCADES depth slices. Each slice is
// enclosed in a bounding sphere and rendered from the sun with an
// orthographic camera into one tile of a shared depth atlas. Fitting a
// sphere (instead of a tight box) makes each cascade's size independent of
// camera rotation; combined with snapping the cascade origin to whole shadow
// texels this removes the "shimmering" edges you get when the camera turns.
//
// For each pixel we pick the first (highest resolution) cascade whose tile
// contains it, and blend into the next cascade near the tile border to hide
// the resolution seam.
//
// Acne/peter-panning control:
//   * normal offset: the lookup position is pushed along the surface normal
//     by ~1-3 shadow texels (more at grazing angles),
//   * a small constant depth bias proportional to texel size.
//
// SOFT SHADOWS (PCF)
// ------------------
// The atlas is sampled through a sampler2DShadow, so every tap is already a
// hardware 2x2 bilinear depth comparison. We take PCF taps on a Vogel disk
// rotated per pixel by interleaved gradient noise: banding turns into fine
// noise that FXAA/temporal accumulation smooths out.

#ifndef CSM_CASCADES
#define CSM_CASCADES 0
#endif
#ifndef PCF_TAPS
#define PCF_TAPS 8
#endif
#ifndef LOCAL_PCF_TAPS
#define LOCAL_PCF_TAPS 6
#endif

#if CSM_CASCADES > 0
uniform sampler2DShadow u_csmAtlas;
uniform mat4 u_csmMat[CSM_CASCADES];    // world -> atlas (u, v, depth)
uniform vec4 u_csmRect[CSM_CASCADES];   // tile rectangle in atlas UV (u0, v0, u1, v1)
uniform vec4 u_csmParams[CSM_CASCADES]; // x: world size of one texel, y: depth units per metre
uniform vec4 u_csmInfo;                 // x: 1/atlas size, y: filter radius (texels), z: max distance, w: fade length

float pcfCascade(int i, vec3 uvz, float rotation) {
    float radius = u_csmInfo.y * u_csmInfo.x;
    vec4 rect = u_csmRect[i];
    float sum = 0.0;
    for (int k = 0; k < PCF_TAPS; ++k) {
        vec2 offs = vogelDisk(k, PCF_TAPS, rotation) * radius;
        vec2 uv = clamp(uvz.xy + offs, rect.xy, rect.zw);
        sum += texture(u_csmAtlas, vec3(uv, uvz.z));
    }
    return sum / float(PCF_TAPS);
}

vec3 cascadeCoord(int i, vec3 P, vec3 N, float NoL) {
    float texel = u_csmParams[i].x;
    // push the lookup point off the surface; more when the light grazes it
    float offset = texel * (1.2 + 2.0 * (1.0 - NoL));
    vec3 uvz = (u_csmMat[i] * vec4(P + N * offset, 1.0)).xyz;
    uvz.z -= u_csmParams[i].y * texel * 0.6;
    return uvz;
}

// 1 = fully lit, 0 = fully shadowed
float sunShadow(vec3 P, vec3 N, float NoL, float viewDist, vec2 pixel) {
    if (viewDist > u_csmInfo.z) return 1.0;
    float rotation = interleavedGradientNoise(pixel) * 6.2831853;
    float margin = (u_csmInfo.y + 2.0) * u_csmInfo.x;
    for (int i = 0; i < CSM_CASCADES; ++i) {
        vec3 uvz = cascadeCoord(i, P, N, NoL);
        vec4 rect = u_csmRect[i];
        vec2 lo = rect.xy + margin;
        vec2 hi = rect.zw - margin;
        if (all(greaterThan(uvz.xy, lo)) && all(lessThan(uvz.xy, hi)) && uvz.z < 1.0) {
            float s = pcfCascade(i, uvz, rotation);
            // blend toward the next cascade close to this tile's border
            vec2 size = hi - lo;
            vec2 d = min(uvz.xy - lo, hi - uvz.xy) / size;
            float edge = min(d.x, d.y);
            float blend = saturate(1.0 - edge / 0.08);
            if (blend > 0.0 && i + 1 < CSM_CASCADES) {
                vec3 uvz2 = cascadeCoord(i + 1, P, N, NoL);
                s = mix(s, pcfCascade(i + 1, uvz2, rotation), blend);
            } else if (blend > 0.0) {
                s = mix(s, 1.0, blend);
            }
            float fade = saturate((u_csmInfo.z - viewDist) / max(u_csmInfo.w, 1e-3));
            return mix(1.0, s, fade);
        }
    }
    return 1.0;
}
#else
float sunShadow(vec3 P, vec3 N, float NoL, float viewDist, vec2 pixel) { return 1.0; }
#endif

// ------------------------------------------------------------- local lights
// Spot lights use one atlas tile with a perspective projection; point lights
// use six tiles (one per cube face, 90+ degree frusta) and pick the face from
// the dominant axis of the light->pixel vector. Matrices live in a float
// texture (4 texels = 4 columns, 5th texel = tile rect) to avoid uniform limits.
#ifdef LOCAL_SHADOWS
uniform sampler2DShadow u_localShadowAtlas;
uniform sampler2D u_localShadowMats;
uniform vec4 u_localShadowInfo; // x: 1/atlas size, y: filter radius (texels)

int cubeFace(vec3 d) {
    vec3 a = abs(d);
    if (a.x >= a.y && a.x >= a.z) return d.x > 0.0 ? 0 : 1;
    if (a.y >= a.z) return d.y > 0.0 ? 2 : 3;
    return d.z > 0.0 ? 4 : 5;
}

float localShadow(int tile, vec3 P, vec3 N, vec3 L, float dist, float nearPlane, float normalScale, float rotation) {
    vec4 c0 = texelFetch(u_localShadowMats, ivec2(0, tile), 0);
    vec4 c1 = texelFetch(u_localShadowMats, ivec2(1, tile), 0);
    vec4 c2 = texelFetch(u_localShadowMats, ivec2(2, tile), 0);
    vec4 c3 = texelFetch(u_localShadowMats, ivec2(3, tile), 0);
    vec4 rect = texelFetch(u_localShadowMats, ivec2(4, tile), 0);
    float NoL = saturate(dot(N, L));
    vec4 p = vec4(P + N * (dist * normalScale * (1.0 + 2.0 * (1.0 - NoL))), 1.0);
    vec4 clip = vec4(dot(p, c0), dot(p, c1), dot(p, c2), dot(p, c3));
    if (clip.w <= 0.0) return 1.0;
    vec3 uvz = clip.xyz / clip.w;
    // perspective depth: convert ~2cm of world bias into depth-buffer units
    uvz.z -= 0.02 * nearPlane / max(dist * dist, 1e-4);
    float radius = u_localShadowInfo.y * u_localShadowInfo.x;
    vec2 lo = rect.xy + u_localShadowInfo.x;
    vec2 hi = rect.zw - u_localShadowInfo.x;
    float sum = 0.0;
    for (int k = 0; k < LOCAL_PCF_TAPS; ++k) {
        vec2 uv = clamp(uvz.xy + vogelDisk(k, LOCAL_PCF_TAPS, rotation) * radius, lo, hi);
        sum += texture(u_localShadowAtlas, vec3(uv, uvz.z));
    }
    return sum / float(LOCAL_PCF_TAPS);
}
#endif
