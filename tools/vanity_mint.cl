/* ed25519 vanity grinder, OpenCL.
 *
 * One work item = one candidate mint. seed = prefix(24) || batch(4) || gid(4);
 * scalar = clamp(SHA-512(seed)[:32]); A = scalar * B by 32 byte-windows over a
 * host-built table of k * 2^(8i) * B in Niels form (y+x, y-x, 2dxy); then the
 * packed public key's base58 tail is checked as (key mod 58^n).
 *
 * Field elements follow ref10: ten int limbs, alternating 26 and 25 bits,
 * products in 64-bit. The host passes SHA-512 constants and the table, and
 * cross-checks every result against the repo's Python ed25519 before it is
 * kept, so nothing here is trusted on its own.
 */

/* ---- SHA-512 ------------------------------------------------------------ */

#define ROTR(x, n) (((x) >> (n)) | ((x) << (64 - (n))))
#define CH(x, y, z) (((x) & (y)) ^ (~(x) & (z)))
#define MAJ(x, y, z) (((x) & (y)) ^ ((x) & (z)) ^ ((y) & (z)))
#define S0(x) (ROTR(x, 28) ^ ROTR(x, 34) ^ ROTR(x, 39))
#define S1(x) (ROTR(x, 14) ^ ROTR(x, 18) ^ ROTR(x, 41))
#define s0(x) (ROTR(x, 1) ^ ROTR(x, 8) ^ ((x) >> 7))
#define s1(x) (ROTR(x, 19) ^ ROTR(x, 61) ^ ((x) >> 6))

__constant ulong SHA_K[80] = { SHA512_K_LITERALS };
__constant ulong SHA_H0[8] = { SHA512_H0_LITERALS };

/* SHA-512 of exactly 32 bytes: one block. */
static void sha512_32(const uchar *m, uchar *out) {
    ulong w[80];
    for (int i = 0; i < 4; i++) {
        ulong v = 0;
        for (int j = 0; j < 8; j++) v = (v << 8) | m[i * 8 + j];
        w[i] = v;
    }
    w[4] = (ulong)0x80 << 56;
    for (int i = 5; i < 15; i++) w[i] = 0;
    w[15] = 256;
    for (int i = 16; i < 80; i++)
        w[i] = s1(w[i - 2]) + w[i - 7] + s0(w[i - 15]) + w[i - 16];
    ulong a = SHA_H0[0], b = SHA_H0[1], c = SHA_H0[2], d = SHA_H0[3];
    ulong e = SHA_H0[4], f = SHA_H0[5], g = SHA_H0[6], h = SHA_H0[7];
    for (int i = 0; i < 80; i++) {
        ulong t1 = h + S1(e) + CH(e, f, g) + SHA_K[i] + w[i];
        ulong t2 = S0(a) + MAJ(a, b, c);
        h = g; g = f; f = e; e = d + t1; d = c; c = b; b = a; a = t1 + t2;
    }
    ulong st[8] = { SHA_H0[0] + a, SHA_H0[1] + b, SHA_H0[2] + c, SHA_H0[3] + d,
                    SHA_H0[4] + e, SHA_H0[5] + f, SHA_H0[6] + g, SHA_H0[7] + h };
    for (int i = 0; i < 8; i++)
        for (int j = 0; j < 8; j++) out[i * 8 + j] = (uchar)(st[i] >> (56 - 8 * j));
}

/* ---- field arithmetic mod 2^255 - 19 ------------------------------------ */

static void fe_copy(int *h, const int *f) { for (int i = 0; i < 10; i++) h[i] = f[i]; }
static void fe_0(int *h) { for (int i = 0; i < 10; i++) h[i] = 0; }
static void fe_1(int *h) { fe_0(h); h[0] = 1; }
static void fe_add(int *h, const int *f, const int *g) { for (int i = 0; i < 10; i++) h[i] = f[i] + g[i]; }
static void fe_sub(int *h, const int *f, const int *g) { for (int i = 0; i < 10; i++) h[i] = f[i] - g[i]; }

/* ref10's carry step: signed rounding carry out of limb k into k+1 (or,
 * from limb 9, times 19 into limb 0). */
#define CARRY(k, next, bits, mul) do { \
    long c = (t[k] + ((long)1 << ((bits) - 1))) >> (bits); \
    t[next] += c * (mul); \
    t[k] -= c << (bits); \
} while (0)

static void fe_mul(int *h, const int *f, const int *g) {
    long t[19];
    for (int i = 0; i < 19; i++) t[i] = 0;
    for (int i = 0; i < 10; i++) {
        long fi = f[i];
        long fi2 = (i & 1) ? 2 * fi : fi;
        for (int j = 0; j < 10; j++) {
            long m = (j & 1) ? fi2 : fi;
            long gj = (i + j >= 10) ? 19 * (long)g[j] : (long)g[j];
            t[i + j] += m * gj;
        }
    }
    for (int i = 0; i < 9; i++) t[i] += t[i + 10];
    CARRY(0, 1, 26, 1); CARRY(4, 5, 26, 1);
    CARRY(1, 2, 25, 1); CARRY(5, 6, 25, 1);
    CARRY(2, 3, 26, 1); CARRY(6, 7, 26, 1);
    CARRY(3, 4, 25, 1); CARRY(7, 8, 25, 1);
    CARRY(4, 5, 26, 1); CARRY(8, 9, 26, 1);
    CARRY(9, 0, 25, 19);
    CARRY(0, 1, 26, 1);
    for (int i = 0; i < 10; i++) h[i] = (int)t[i];
}

static void fe_sq(int *h, const int *f) { fe_mul(h, f, f); }

static void fe_sqn(int *h, const int *f, int n) {
    fe_copy(h, f);
    for (int i = 0; i < n; i++) fe_sq(h, h);
}

/* z^(p-2), ref10's addition chain. */
static void fe_invert(int *out, const int *z) {
    int t0[10], t1[10], t2[10], t3[10];
    fe_sq(t0, z);                       /* z^2 */
    fe_sqn(t1, t0, 2);                  /* z^8 */
    fe_mul(t1, z, t1);                  /* z^9 */
    fe_mul(t0, t0, t1);                 /* z^11 */
    fe_sq(t2, t0);                      /* z^22 */
    fe_mul(t1, t1, t2);                 /* z^31 = 2^5-1 */
    fe_sqn(t2, t1, 5);  fe_mul(t1, t2, t1);   /* 2^10-1 */
    fe_sqn(t2, t1, 10); fe_mul(t2, t2, t1);   /* 2^20-1 */
    fe_sqn(t3, t2, 20); fe_mul(t2, t3, t2);   /* 2^40-1 */
    fe_sqn(t2, t2, 10); fe_mul(t1, t2, t1);   /* 2^50-1 */
    fe_sqn(t2, t1, 50); fe_mul(t2, t2, t1);   /* 2^100-1 */
    fe_sqn(t3, t2, 100); fe_mul(t2, t3, t2);  /* 2^200-1 */
    fe_sqn(t2, t2, 50); fe_mul(t1, t2, t1);   /* 2^250-1 */
    fe_sqn(t1, t1, 5);                        /* 2^255-32 */
    fe_mul(out, t1, t0);                      /* 2^255-21 */
}

/* Canonical little-endian bytes of a field element (ref10 fe_tobytes). The
 * packing uses fixed shifts into four 64-bit words: a byte-at-a-time loop with
 * a running index was miscompiled by AMD's optimiser (top byte wrong for
 * negative limbs; correct with -cl-opt-disable). */
__constant int FE_OFF[10] = { 0, 26, 51, 77, 102, 128, 153, 179, 204, 230 };

__attribute__((noinline)) static void fe_tobytes(uchar *s, const int *f) {
    long t[10];
    for (int i = 0; i < 10; i++) t[i] = f[i];
    long q = (19 * t[9] + ((long)1 << 24)) >> 25;
    for (int i = 0; i < 10; i++) q = (t[i] + q) >> ((i & 1) ? 25 : 26);
    t[0] += 19 * q;
    for (int i = 0; i < 10; i++) {
        int bits = (i & 1) ? 25 : 26;
        long c = t[i] >> bits;
        if (i < 9) t[i + 1] += c;
        t[i] -= c << bits;
    }
    ulong w0 = 0, w1 = 0, w2 = 0, w3 = 0;
    w0 |= (ulong)t[0];              /* bits 0..25 */
    w0 |= (ulong)t[1] << 26;        /* 26..50 */
    w0 |= (ulong)t[2] << 51;        /* 51..63 low part */
    w1 |= (ulong)t[2] >> 13;        /* 64..76 */
    w1 |= (ulong)t[3] << 13;        /* 77..101 */
    w1 |= (ulong)t[4] << 38;        /* 102..127 */
    w2 |= (ulong)t[5];              /* 128..152 */
    w2 |= (ulong)t[6] << 25;        /* 153..178 */
    w2 |= (ulong)t[7] << 51;        /* 179..191 low part */
    w3 |= (ulong)t[7] >> 13;        /* 192..203 */
    w3 |= (ulong)t[8] << 12;        /* 204..229 */
    w3 |= (ulong)t[9] << 38;        /* 230..254 */
    for (int j = 0; j < 8; j++) {
        s[j] = (uchar)(w0 >> (8 * j));
        s[8 + j] = (uchar)(w1 >> (8 * j));
        s[16 + j] = (uchar)(w2 >> (8 * j));
        s[24 + j] = (uchar)(w3 >> (8 * j));
    }
}

/* Low bit of the canonical value: the same reduction as fe_tobytes without
 * the packing loop, which the AMD compiler mis-optimised when only one byte
 * of its output was used. */
__attribute__((noinline)) static int fe_parity(const int *f) {
    long t[10];
    for (int i = 0; i < 10; i++) t[i] = f[i];
    long q = (19 * t[9] + ((long)1 << 24)) >> 25;
    for (int i = 0; i < 10; i++) q = (t[i] + q) >> ((i & 1) ? 25 : 26);
    t[0] += 19 * q;
    for (int i = 0; i < 10; i++) {
        int bits = (i & 1) ? 25 : 26;
        long c = t[i] >> bits;
        if (i < 9) t[i + 1] += c;
        t[i] -= c << bits;
    }
    return (int)(t[0] & 1);
}

/* ---- the group -------------------------------------------------------------- */

/* P (extended X,Y,Z,T) += q (Niels y+x, y-x, 2dxy), ref10 ge_madd + p1p1_to_p3. */
static void ge_madd(int *X, int *Y, int *Z, int *T, __global const int *q) {
    int ypx[10], ymx[10], xy2d[10];
    for (int i = 0; i < 10; i++) { ypx[i] = q[i]; ymx[i] = q[10 + i]; xy2d[i] = q[20 + i]; }
    int a[10], b[10], c[10], d[10], x3[10], y3[10], z3[10], t3[10];
    fe_add(a, Y, X);   fe_mul(a, a, ypx);
    fe_sub(b, Y, X);   fe_mul(b, b, ymx);
    fe_mul(c, xy2d, T);
    fe_add(d, Z, Z);
    fe_sub(x3, a, b);
    fe_add(y3, a, b);
    fe_add(z3, d, c);
    fe_sub(t3, d, c);
    fe_mul(X, x3, t3);
    fe_mul(Y, y3, z3);
    fe_mul(Z, z3, t3);
    fe_mul(T, x3, y3);
}

/* seed -> packed public key. */
static void pubkey_of(const uchar *seed, __global const int *table, uchar *pk) {
    uchar h[64];
    sha512_32(seed, h);
    h[0] &= 248; h[31] &= 127; h[31] |= 64;
    int X[10], Y[10], Z[10], T[10];
    fe_0(X); fe_1(Y); fe_1(Z); fe_0(T);
    for (int i = 0; i < 32; i++)
        ge_madd(X, Y, Z, T, table + ((i * 256 + h[i]) * 30));
    int zi[10], x[10], y[10];
    fe_invert(zi, Z);
    fe_mul(x, X, zi);
    fe_mul(y, Y, zi);
    fe_tobytes(pk, y);
    pk[31] ^= (uchar)(fe_parity(x) << 7);
}

static void make_seed(uchar *seed, __constant const uchar *prefix, uint batch, uint gid) {
    for (int i = 0; i < 24; i++) seed[i] = prefix[i];
    for (int i = 0; i < 4; i++) { seed[24 + i] = (uchar)(batch >> (8 * i)); seed[28 + i] = (uchar)(gid >> (8 * i)); }
}

/* Test kernel: the public key of every seed, for the host to compare. */
__kernel void pubkeys(__global const int *table, __constant const uchar *prefix, uint batch,
                      __global uchar *out) {
    uint gid = get_global_id(0);
    uchar seed[32], pk[32];
    make_seed(seed, prefix, batch, gid);
    pubkey_of(seed, table, pk);
    for (int i = 0; i < 32; i++) out[gid * 32 + i] = pk[i];
}

/* Grind kernel: keep seeds whose address ends in one of the targets. */
__kernel void grind(__global const int *table, __constant const uchar *prefix, uint batch,
                    __constant const uint *targets, uint ntargets, uint modulus,
                    __global uint *count, __global uchar *found, uint max_found) {
    uint gid = get_global_id(0);
    uchar seed[32], pk[32];
    make_seed(seed, prefix, batch, gid);
    pubkey_of(seed, table, pk);
    ulong r = 0;
    for (int i = 0; i < 32; i++) r = ((r << 8) | pk[i]) % modulus;
    int hit = 0;
    for (uint k = 0; k < ntargets; k++) hit |= (r == targets[k]);
    if (hit) {
        uint idx = atomic_inc(count);
        if (idx < max_found)
            for (int i = 0; i < 32; i++) found[idx * 32 + i] = seed[i];
    }
}

