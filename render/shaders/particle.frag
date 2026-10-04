#version 330
// particle.frag - atlas lookup, tint and (for alpha particles) lighting.
// ADDITIVE particles output HDR colour that is added to the scene with
// blend (src_alpha, one); vertex colours above 1.0 make sparks/flames glow.

uniform sampler2D u_atlas;
// Soft particles: fade where the quad cuts into geometry (depth from the
// pre-pass), so smoke and dust never show a hard line on the floor/walls.
uniform sampler2D u_sceneDepth;
uniform vec4 u_softParams;      // x: 1 / fade distance (1/m), y: near, z: far, w: 1 = enabled

float softFade() {
    if (u_softParams.w < 0.5) return 1.0;
    float n = u_softParams.y;
    float f = u_softParams.z;
    float d = texelFetch(u_sceneDepth, ivec2(gl_FragCoord.xy), 0).r;
    float sceneZ = 2.0 * n * f / (f + n - (d * 2.0 - 1.0) * (f - n));
    float partZ = 2.0 * n * f / (f + n - (gl_FragCoord.z * 2.0 - 1.0) * (f - n));
    return clamp((sceneZ - partZ) * u_softParams.x, 0.0, 1.0);
}

in vec2 v_uv;
in vec4 v_color;
in vec3 v_light;

layout(location = 0) out vec4 o_color;

void main() {
    vec4 t = texture(u_atlas, v_uv);
    vec4 c = t * v_color;
    float soft = softFade();
#ifdef ADDITIVE
    o_color = vec4(c.rgb * soft, c.a);
#else
    c.a *= soft;
    if (c.a < 0.003) discard;
    o_color = vec4(c.rgb * v_light, c.a);
#endif
}
