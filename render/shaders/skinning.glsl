// skinning.glsl - linear blend skinning for the soldier bodies.
//
// Every soldier shares one 47-bone skeleton (gameplay/skeleton.py). Its
// meshes are stored in the bind pose (an A-pose), and each vertex names up
// to four bones with weights that add up to 1. Once per tick the CPU poses
// the skeleton and uploads, per bone, the matrix that takes a bind-pose
// point to where that bone has moved it: inverse(bind) then current.
//
// The palette is three vec4 rows per bone: the top three rows of the affine
// matrix (the fourth row is always 0 0 0 1). 48 bones x 3 = 144 vec4, which
// is well inside the 1024 vertex-uniform components GL 3.3 guarantees; full
// mat4s would need a third more. Blending the rows by weight blends the
// matrices, then the vertex is moved once.
//
// The same include serves pbr.vert (main pass), prepass.vert (depth and
// normal pre-pass) and depth.vert (every shadow cascade and local shadow),
// so all passes agree on the pose. Rigid meshes (the jointed mannequin,
// props on a bone) carry weight 1 on one bone. Everything else in the
// world is drawn with u_skinned = 0 (set on the scene root) and never
// reads the skin attributes.
const int MAX_BONES = 48;
uniform vec4 u_bones[MAX_BONES * 3];
uniform float u_skinned;
in vec4 skin_joints;
in vec4 skin_weights;

mat4 skin_matrix() {
    if (u_skinned < 0.5) {
        return mat4(1.0);
    }
    vec4 r0 = vec4(0.0);
    vec4 r1 = vec4(0.0);
    vec4 r2 = vec4(0.0);
    for (int i = 0; i < 4; ++i) {
        float w = skin_weights[i];
        int b = int(skin_joints[i] + 0.5) * 3;
        r0 += w * u_bones[b];
        r1 += w * u_bones[b + 1];
        r2 += w * u_bones[b + 2];
    }
    // r0..r2 are rows; mat4() takes columns, hence the transpose
    return transpose(mat4(r0, r1, r2, vec4(0.0, 0.0, 0.0, 1.0)));
}
