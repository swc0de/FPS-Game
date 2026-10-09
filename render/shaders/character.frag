#version 330
// character.frag - soldier bodies: skin, eyes, hair, cloth and gear in one draw.
//
// Every vertex names a palette slot (characters/palette.py). A body's slot
// table is two uniform arrays: colour + roughness, and the shading kind with
// its parameters. Skin, hair, eyes, clothing and gear therefore share one
// mesh and one draw call, a team's uniform is a different table (no new
// geometry at halftime), and the shading model is chosen per fragment:
//
//   skin    wrap-lit diffuse with a red-shifted terminator, a cheap stand-in
//           for subsurface scattering (light enters the skin, scatters and
//           leaves a little past where it hit; "pre-integrated skin",
//           Penner 2011, reduced to a wrap term tinted by the scattering
//           colour), plus a two-lobe specular: a broad oily sheen and a
//           tight one; pores in the detail normal;
//   fabric  Lambert with a little wrap (light bleeds through weave) plus the
//           "Charlie" sheen lobe (Estevez & Kulla 2017): fibres standing out
//           of the cloth catch light at grazing angles, which is what makes
//           cloth look soft at the silhouette; low specular otherwise;
//   hard    the standard GGX BRDF (brdf.glsl): polymer, paint, rubber;
//   eye     a glossy cornea over a matte iris and sclera;
//   hair    rough GGX with strand noise along the scalp;
//   lens    near-black, very glossy.
//
// Detail (pores, weave, scuffs) is a tiled normal map projected in BIND
// space from three axes ("triplanar", blended by the bind-pose normal), so
// it follows the skin through any pose without UVs. Vertex colour carries
// painted variation (lips, brows, iris, stubble) multiplied over the slot
// colour, and its alpha the cavity occlusion baked from the distance field.
// Wear: grime gathers in cavities and on the lower legs and hands.

#include "common.glsl"
#include "brdf.glsl"
#include "shadows.glsl"
#include "lights.glsl"
#include "ibl.glsl"
#include "fog.glsl"
#include "ssao.glsl"

const int N_SLOTS = 24;
uniform vec4 u_palette[N_SLOTS];      // rgb: linear colour, a: roughness
uniform vec4 u_slotParams[N_SLOTS];   // x: kind, y: metallic, z: sheen, w: wear
uniform sampler2D u_detail;           // 2x2 tiles: skin, fabric, hard, hair; rg: normal xy, b: cavity, a: grime
uniform vec4 u_emission;
uniform vec4 p3d_ColorScale;
uniform vec3 u_camPos;
uniform vec3 u_sunDir;
uniform vec3 u_sunColor;

in vec3 v_worldPos;
in vec3 v_normal;
in vec3 v_bindPos;
in vec3 v_bindNormal;
in mat3 v_bindToWorld;
in vec4 v_color;
flat in int v_slot;

layout(location = 0) out vec4 o_color;

const int KIND_SKIN = 0;
const int KIND_FABRIC = 1;
const int KIND_HARD = 2;
const int KIND_EYE = 3;
const int KIND_HAIR = 4;
const int KIND_LENS = 5;

// detail tile for a kind, sampled at a given scale (metres per tile)
vec4 detailSample(vec2 uv, int tile) {
    vec2 o = vec2(float(tile & 1), float(tile >> 1)) * 0.5;
    return texture(u_detail, o + fract(uv) * 0.5);
}

// Triplanar detail in bind space: returns (perturbed bind-space normal, cavity, grime)
vec4 triplanar(vec3 p, vec3 n, int tile, float scale, float strength, out float grime) {
    vec3 w = pow(abs(n), vec3(4.0));
    w /= (w.x + w.y + w.z + 1e-5);
    vec4 sx = detailSample(p.yz / scale, tile);
    vec4 sy = detailSample(p.xz / scale, tile);
    vec4 sz = detailSample(p.xy / scale, tile);
    // whiteout blend: tangent-space xy from each projection added onto the matching axes
    vec2 tx = (sx.rg * 2.0 - 1.0) * strength;
    vec2 ty = (sy.rg * 2.0 - 1.0) * strength;
    vec2 tz = (sz.rg * 2.0 - 1.0) * strength;
    vec3 nx = vec3(0.0, tx.x, tx.y) * sign(n.x);
    vec3 ny = vec3(ty.x, 0.0, ty.y) * sign(n.y);
    vec3 nz = vec3(tz.x, tz.y, 0.0) * sign(n.z);
    vec3 nb = normalize(n + nx * w.x + ny * w.y + nz * w.z);
    float cav = sx.b * w.x + sy.b * w.y + sz.b * w.z;
    grime = sx.a * w.x + sy.a * w.y + sz.a * w.z;
    return vec4(nb, cav);
}

float D_Charlie(float NoH, float a) {
    float inv = 1.0 / max(a, 0.05);
    float s2 = max(1.0 - NoH * NoH, 0.0078125);
    return (2.0 + inv) * pow(s2, inv * 0.5) / (2.0 * PI);
}

float V_Neubelt(float NoV, float NoL) {
    return saturate(1.0 / (4.0 * (NoL + NoV - NoL * NoV)));
}

// direct light for the character kinds; ``wrap`` softens the terminator
vec3 shadeCharacter(Surface s, int kind, vec3 L, vec3 radiance, float sheen, float shadow) {
    float NoLr = dot(s.N, L);
    vec3 H = normalize(L + s.V);
    float NoH = saturate(dot(s.N, H));
    float VoH = saturate(dot(s.V, H));
    float NoL = saturate(NoLr);
    if (kind == KIND_SKIN) {
        // wrapped diffuse with the scattering colour showing at the terminator
        float w = 0.45;
        float wrapped = saturate((NoLr + w) / (1.0 + w));
        vec3 scatter = vec3(1.0, 0.36, 0.25);
        vec3 diffTerm = mix(scatter * wrapped, vec3(wrapped), saturate(NoL * 1.5)) * shadow;
        vec3 diff = s.diffuse * (1.0 / PI) * diffTerm;
        // two specular lobes: broad sheen and tight highlight, skin f0 ~ 0.028
        vec3 F = F_Schlick(vec3(0.028), VoH);
        float a1 = max(s.alpha, 0.2), a2 = max(s.alpha * 0.4, 0.06);
        float spec = 0.85 * D_GGX(NoH, a1) * V_SmithGGXCorrelated(s.NoV, NoL, a1)
                   + 0.15 * D_GGX(NoH, a2) * V_SmithGGXCorrelated(s.NoV, NoL, a2);
        return (diff + spec * F * NoL * shadow) * radiance;
    }
    if (kind == KIND_FABRIC) {
        float w = 0.2;
        float wrapped = saturate((NoLr + w) / (1.0 + w));
        vec3 diff = s.diffuse * (1.0 / PI) * wrapped;
        vec3 sheenCol = mix(s.diffuse, vec3(1.0), 0.35) * sheen;
        vec3 sheenTerm = sheenCol * D_Charlie(NoH, s.roughness) * V_Neubelt(s.NoV, NoL) * NoL;
        vec3 F = F_Schlick(vec3(0.03), VoH);
        float spec = D_GGX(NoH, s.alpha) * V_SmithGGXCorrelated(s.NoV, NoL, s.alpha) * NoL;
        return (diff + sheenTerm + spec * F) * radiance * shadow;
    }
    return shadeDirect(s, L, radiance * shadow);
}

void main() {
    int slot = clamp(v_slot, 0, N_SLOTS - 1);
    vec4 pal = u_palette[slot];
    vec4 prm = u_slotParams[slot];
    int kind = int(prm.x + 0.5);

    vec3 Ng = normalize(v_normal);
    if (!gl_FrontFacing) Ng = -Ng;
    vec3 V = normalize(u_camPos - v_worldPos);

    // detail per kind: tile, scale (metres per tile), strength
    int tile = kind == KIND_SKIN ? 0 : (kind == KIND_FABRIC ? 1 : (kind == KIND_HAIR ? 3 : 2));
    float scale = kind == KIND_SKIN ? 0.035 : (kind == KIND_FABRIC ? 0.05 : (kind == KIND_HAIR ? 0.03 : 0.12));
    float strength = kind == KIND_SKIN ? 0.35 : (kind == KIND_FABRIC ? 0.6 : (kind == KIND_HAIR ? 0.8 : 0.3));
    if (kind == KIND_EYE || kind == KIND_LENS) strength = 0.0;
    float grime = 0.0;
    vec4 det = triplanar(v_bindPos, normalize(v_bindNormal), tile, scale, strength, grime);
    vec3 N = normalize(v_bindToWorld * det.xyz);
    if (!gl_FrontFacing) N = -N;

    vec3 base = pal.rgb * v_color.rgb * p3d_ColorScale.rgb;
    float cavity = v_color.a;
    float roughness = pal.a;
    // wear: grime in cavities, on the lower legs and the hands; a little on all cloth and gear
    float wear = prm.w;
    if (wear > 0.0) {
        float low = 1.0 - smoothstep(0.15, 0.6, v_bindPos.z);
        float g = saturate(grime * (0.35 + 0.65 * (1.0 - cavity)) + low * 0.5 * grime) * wear;
        base = mix(base, base * vec3(0.62, 0.58, 0.52), g);
        roughness = mix(roughness, 1.0, g * 0.5);
    }
    if (kind == KIND_HAIR) {
        base *= mix(0.75, 1.15, grime);
    }

    Surface s;
    s.P = v_worldPos;
    s.N = N;
    s.V = V;
    s.NoV = max(dot(N, V), 1e-4);
    float metallic = prm.y;
    s.diffuse = base * (1.0 - metallic);
    s.f0 = mix(vec3(kind == KIND_EYE ? 0.025 : 0.04), base, metallic);
    s.roughness = clamp(roughness * (0.85 + 0.3 * det.w), 0.04, 1.0);
    s.alpha = s.roughness * s.roughness;
    s.ao = cavity;

    vec2 pixel = gl_FragCoord.xy;
    float viewDist = length(u_camPos - v_worldPos);

    vec3 color = vec3(0.0);
    float NgoL = dot(Ng, u_sunDir);
    if (NgoL > -0.35) {
        float sh = sunShadow(v_worldPos, Ng, saturate(NgoL), viewDist, pixel);
        float micro = saturate(dot(N, u_sunDir) + 2.0 * cavity * cavity - 1.0 + (kind == KIND_SKIN ? 0.4 : 0.0));
        color += shadeCharacter(s, kind, u_sunDir, u_sunColor * micro, prm.z, sh);
    }
    float ssao = screenSpaceAO();
#ifdef SSAO
    color += shadeLocalLights(s, pixel) * mix(1.0, ssao, u_ssaoParams.w);
#else
    color += shadeLocalLights(s, pixel);
#endif
    color += ambientLighting(s, Ng, cavity * ssao);
    if (kind == KIND_FABRIC) {
        // sheen also lifts the ambient at grazing angles
        color += base * shIrradiance(N) * u_iblParams.x * prm.z * pow(1.0 - s.NoV, 4.0) * 0.6 * cavity;
    }
    color += u_emission.rgb;

#ifdef FOG
    color = applyFog(color, u_camPos, v_worldPos, u_sunDir);
#endif
    o_color = vec4(color, 1.0);
}
