
-- ABI word helpers. h = upper-case hex(data); a word is a 64-char hex string.
CREATE OR REPLACE MACRO w_u128(w) AS ((('0x' || substr(w, 33, 16))::UBIGINT)::UHUGEINT * 18446744073709551616::UHUGEINT + (('0x' || substr(w, 49, 16))::UBIGINT)::UHUGEINT);
CREATE OR REPLACE MACRO w_hi0(w) AS (substr(w, 1, 32) = '00000000000000000000000000000000');
CREATE OR REPLACE MACRO w_hiF(w) AS (substr(w, 1, 32) = 'FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF');
-- unsigned -> HUGEINT (NULL if >= 2^127, i.e. not representable; w_dbl keeps magnitude)
CREATE OR REPLACE MACRO w_uint(w) AS (CASE WHEN w IS NULL OR length(w) < 64 THEN NULL WHEN w_hi0(w) THEN TRY_CAST(w_u128(w) AS HUGEINT) ELSE NULL END);
-- two's complement int256 -> HUGEINT (NULL outside +-2^127)
CREATE OR REPLACE MACRO w_int(w) AS (CASE WHEN w IS NULL OR length(w) < 64 THEN NULL
    WHEN w_hi0(w) THEN TRY_CAST(w_u128(w) AS HUGEINT)
    WHEN w_hiF(w) THEN -TRY_CAST((340282366920938463463374607431768211455::UHUGEINT - w_u128(w)) + 1::UHUGEINT AS HUGEINT)
    ELSE NULL END);
CREATE OR REPLACE MACRO w_dbl(w) AS (('0x' || substr(w, 1, 16))::UBIGINT::DOUBLE * 6.277101735386681e57 + ('0x' || substr(w, 17, 16))::UBIGINT::DOUBLE * 3.402823669209385e38 + ('0x' || substr(w, 33, 16))::UBIGINT::DOUBLE * 1.8446744073709552e19 + ('0x' || substr(w, 49, 16))::UBIGINT::DOUBLE);
CREATE OR REPLACE MACRO blob_uint(b) AS w_uint(hex(b));
CREATE OR REPLACE MACRO blob_dbl(b) AS w_dbl(hex(b));
CREATE OR REPLACE MACRO a2s(b) AS ('0x' || lower(hex(b)));
-- dynamic string at head word index i (0-based) of data hex h
CREATE OR REPLACE MACRO abi_string(h, i) AS (
  CASE WHEN length(h) >= 64 * (i + 1) THEN
    TRY(decode(unhex(substr(h, CAST(w_uint(substr(h, 64 * i + 1, 64)) AS BIGINT) * 2 + 65,
        CAST(w_uint(substr(h, CAST(w_uint(substr(h, 64 * i + 1, 64)) AS BIGINT) * 2 + 1, 64)) AS BIGINT) * 2))))
  END);
