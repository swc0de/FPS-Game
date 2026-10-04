#version 330
// particle.frag - atlas lookup, tint and (for alpha particles) lighting.
// ADDITIVE particles output HDR colour that is added to the scene with
// blend (src_alpha, one); vertex colours above 1.0 make sparks/flames glow.

uniform sampler2D u_atlas;

in vec2 v_uv;
in vec4 v_color;
in vec3 v_light;

layout(location = 0) out vec4 o_color;

void main() {
    vec4 t = texture(u_atlas, v_uv);
    vec4 c = t * v_color;
#ifdef ADDITIVE
    o_color = vec4(c.rgb, c.a);
#else
    if (c.a < 0.003) discard;
    o_color = vec4(c.rgb * v_light, c.a);
#endif
}
