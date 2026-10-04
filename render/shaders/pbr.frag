#version 330
// pbr.frag - forward PBR surface shader.
//
// Per pixel:
//   1. (optional) parallax occlusion mapping offsets the UV using the
//      height stored in the normal map's alpha channel,
//   2. sample albedo / normal / ORM (occlusion-roughness-metallic) maps,
//   3. specular anti-aliasing widens roughness where the normal changes fast
//      between pixels (Tokuyoshi & Kaplanyan 2019) to stop sparkling,
//   4. sun light with cascaded soft shadows,
//   5. local point/spot lights with atlas shadows,
//   6. image based ambient light (SH diffuse + prefiltered specular),
//      occluded by the baked sky-visibility volume and by screen-space
//      ambient occlusion (GTAO, computed from the depth/normal pre-pass),
//   7. height fog. Output is linear HDR; tone mapping happens in post.

#include "common.glsl"
#include "brdf.glsl"
#include "shadows.glsl"
#include "lights.glsl"
#include "ibl.glsl"
#include "fog.glsl"
#include "ssao.glsl"

uniform sampler2D u_albedo;
uniform sampler2D u_normalMap;
uniform sampler2D u_orm;
uniform vec4 u_matParams;      // x: parallax scale, y: normal strength, z: roughness mul, w: metallic mul
uniform vec4 u_emission;       // rgb: emitted radiance (light fixtures), a: unused
uniform vec4 p3d_ColorScale;   // per-object tint

uniform vec3 u_camPos;
uniform vec3 u_sunDir;         // unit vector toward the sun
uniform vec3 u_sunColor;       // sun radiance (colour * intensity)

in vec3 v_worldPos;
in vec3 v_normal;
in vec3 v_tangent;
in vec3 v_binormal;
in vec2 v_uv;

layout(location = 0) out vec4 o_color;

#ifdef PARALLAX
// Parallax occlusion mapping: march the view ray through the height field in
// tangent space and intersect it (linear search + one refinement step).
vec2 parallaxUV(vec2 uv, vec3 Vts, float scale) {
    float layers = mix(24.0, 8.0, abs(Vts.z));
    float layerDepth = 1.0 / layers;
    vec2 delta = Vts.xy / max(Vts.z, 0.15) * scale / layers;
    float current = 0.0;
    vec2 cuv = uv;
    float h = 1.0 - texture(u_normalMap, cuv).a;
    for (int i = 0; i < 32; ++i) {
        if (current >= h) break;
        cuv -= delta;
        h = 1.0 - texture(u_normalMap, cuv).a;
        current += layerDepth;
    }
    vec2 prev = cuv + delta;
    float after = h - current;
    float before = (1.0 - texture(u_normalMap, prev).a) - current + layerDepth;
    float w = after / (after - before + 1e-5);
    return mix(cuv, prev, w);
}
#endif

void main() {
    vec3 Ng = normalize(v_normal);
    if (!gl_FrontFacing) Ng = -Ng;
    vec3 T = normalize(v_tangent - Ng * dot(Ng, v_tangent));
    vec3 B = normalize(v_binormal - Ng * dot(Ng, v_binormal) - T * dot(T, v_binormal));
    mat3 TBN = mat3(T, B, Ng);
    vec3 V = normalize(u_camPos - v_worldPos);
    vec2 uv = v_uv;

#ifdef PARALLAX
    if (u_matParams.x > 0.0) {
        vec3 Vts = transpose(TBN) * V;
        float fade = saturate(1.0 - (length(u_camPos - v_worldPos) - 6.0) / 10.0);
        if (fade > 0.0) uv = parallaxUV(uv, Vts, u_matParams.x * fade);
    }
#endif

    vec4 albedoSample = texture(u_albedo, uv);
    vec4 nrm = texture(u_normalMap, uv);
    vec3 orm = texture(u_orm, uv).rgb;

    vec3 baseColor = albedoSample.rgb * p3d_ColorScale.rgb;
    vec3 nts = nrm.xyz * 2.0 - 1.0;
    nts.xy *= u_matParams.y;
    vec3 N = normalize(TBN * nts);

    float ao = orm.r;
    float roughness = clamp(orm.g * u_matParams.z, 0.03, 1.0);
    float metallic = saturate(orm.b * u_matParams.w);

    Surface s;
    s.P = v_worldPos;
    s.N = N;
    s.V = V;
    s.NoV = max(dot(N, V), 1e-4);
    s.diffuse = baseColor * (1.0 - metallic);
    s.f0 = mix(vec3(0.04), baseColor, metallic);
    s.roughness = roughness;
    float alpha = roughness * roughness;
#ifdef SPECULAR_AA
    vec3 dndu = dFdx(N);
    vec3 dndv = dFdy(N);
    float variance = 0.25 * (dot(dndu, dndu) + dot(dndv, dndv));
    float kernel = min(2.0 * variance, 0.18);
    alpha = sqrt(saturate(alpha * alpha + kernel));
#endif
    s.alpha = alpha;
    s.ao = ao;

    vec2 pixel = gl_FragCoord.xy;
    float viewDist = length(u_camPos - v_worldPos);

    // --- sun
    vec3 color = vec3(0.0);
    float NoLsun = dot(N, u_sunDir);
    float NgoL = dot(Ng, u_sunDir);
    if (NoLsun > 0.0 && NgoL > -0.1) {
        float sh = sunShadow(v_worldPos, Ng, saturate(NgoL), viewDist, pixel);
        // micro-shadowing from AO keeps crevices dark even in direct light
        float micro = saturate(NoLsun + 2.0 * ao * ao - 1.0);
        color += shadeDirect(s, u_sunDir, u_sunColor * sh * micro);
    }

    float ssao = screenSpaceAO();

    // --- local lights (a little AO adds contact darkening under lamps)
#ifdef SSAO
    color += shadeLocalLights(s, pixel) * mix(1.0, ssao, u_ssaoParams.w);
#else
    color += shadeLocalLights(s, pixel);
#endif

    // --- ambient / IBL
    color += ambientLighting(s, Ng, ao * ssao);

    // --- emission
    color += u_emission.rgb;

#ifdef FOG
    color = applyFog(color, u_camPos, v_worldPos, u_sunDir);
#endif

#ifdef DECAL
    // decals blend over the surface they sit on using the atlas alpha
    o_color = vec4(color, albedoSample.a);
#else
    o_color = vec4(color, 1.0);
#endif
}
