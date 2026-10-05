// skinning.glsl - rigid GPU skinning for the soldier bodies.
//
// A soldier is ~20 box/sphere parts on a procedural skeleton. Drawing each
// part as its own node costs one draw call per part per pass (main, depth
// pre-pass, every shadow cascade). Instead the body is merged into one mesh
// per material where every vertex carries the index of the part (bone) it
// belongs to, and the parts' current transforms relative to the body root
// are uploaded as a matrix array once per tick (gameplay/body.py). Each
// vertex is moved by exactly one bone (rigid skinning, no blending), which
// is all a jointed mannequin needs.
//
// Everything else in the world is drawn with u_skinned = 0 (set on the
// scene root), so the same shaders handle both.
const int MAX_BONES = 24;
uniform mat4 u_bones[MAX_BONES];
uniform float u_skinned;
in float bone;

mat4 skin_matrix() {
    if (u_skinned < 0.5) {
        return mat4(1.0);
    }
    return u_bones[int(bone + 0.5)];
}
