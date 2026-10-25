/* Average-cost trade ledger per (wallet, token) group: realized/windowed PnL, round trips, transfer adjustment.
 * Rows must be sorted by (gid, t, bn, log order). Buys add to inventory at cost; a sell realizes against the
 * average cost of the swap-bought inventory. Units sold beyond that inventory came from transfers (or unpriced
 * swaps): they realize nothing (valued at the sell price) and are counted in xin_q / xin_u.
 * A round trip opens on the first buy from a flat position and closes when the position is sold back to flat.
 * build: cc -O2 -shared -fPIC -o libpnl.so pnl_kernel.c */
#include <stdint.h>
#include <math.h>

typedef struct {
    int64_t *nb, *ns, *n30, *n7, *trips, *wins, *first_buy_t, *first_t, *last_t, *flips, *open_t;
    double *bq, *sq, *bu, *su, *real_all, *real30, *real7, *bu30, *su30, *bu7, *su7, *bq30, *bq7,
           *qty_end, *cost_end, *xin_q, *xin_u, *hold_sum, *first_buy_px, *max_gain, *win_sum, *loss_sum;
} Out;

int64_t ledger(int64_t n, const int64_t *gid, const int64_t *t, const int64_t *bn, const uint8_t *buy,
               const double *q, const double *u, int64_t t30, int64_t t7, int64_t G, Out *o) {
    int64_t g = -1, last_buy_bn = -1, trip_open = -1;
    double qty = 0, cost = 0, trip_real = 0, trip_peak = 0;
    for (int64_t i = 0; i < n; i++) {
        if (gid[i] != g) {
            if (g >= 0) { o->qty_end[g] = qty; o->cost_end[g] = cost; o->open_t[g] = qty > 0 ? trip_open : -1; }
            g = gid[i];
            if (g < 0 || g >= G) return -1;
            qty = cost = trip_real = trip_peak = 0; last_buy_bn = -1; trip_open = -1;
            o->first_t[g] = t[i]; o->first_buy_t[g] = -1;
        }
        o->last_t[g] = t[i];
        double qi = q[i], ui = u[i];
        if (!(qi > 0) || !(ui >= 0) || !isfinite(qi) || !isfinite(ui)) continue;
        int w30 = t[i] >= t30, w7 = t[i] >= t7;
        if (w30) o->n30[g]++;
        if (w7) o->n7[g]++;
        if (buy[i]) {
            if (qty <= 0) { trip_open = t[i]; trip_real = 0; trip_peak = 0; }
            qty += qi; cost += ui;
            if (qty > trip_peak) trip_peak = qty;
            o->nb[g]++; o->bq[g] += qi; o->bu[g] += ui;
            if (w30) { o->bu30[g] += ui; o->bq30[g] += qi; }
            if (w7) { o->bu7[g] += ui; o->bq7[g] += qi; }
            if (o->first_buy_t[g] < 0) { o->first_buy_t[g] = t[i]; o->first_buy_px[g] = ui / qi; }
            last_buy_bn = bn[i];
        } else {
            o->ns[g]++; o->sq[g] += qi; o->su[g] += ui;
            if (w30) o->su30[g] += ui;
            if (w7) o->su7[g] += ui;
            if (bn[i] == last_buy_bn) o->flips[g]++;
            double cov = qi < qty ? qi : qty;
            if (cov > 0) {
                double avg = cost / qty, r = ui * (cov / qi) - cov * avg;
                o->real_all[g] += r;
                if (w30) o->real30[g] += r;
                if (w7) o->real7[g] += r;
                if (r > o->max_gain[g]) o->max_gain[g] = r;
                cost -= cov * avg; qty -= cov; trip_real += r;
                if (qty <= 1e-5 * trip_peak || qty <= 0) {  /* dust left by float32 amounts closes the trip */
                    qty = 0; cost = 0;
                    o->trips[g]++;
                    if (trip_real > 0) { o->wins[g]++; o->win_sum[g] += trip_real; } else o->loss_sum[g] += trip_real;
                    if (trip_open >= 0) o->hold_sum[g] += (double)(t[i] - trip_open);
                    trip_open = -1;
                }
            }
            if (qi > cov) { o->xin_q[g] += qi - cov; o->xin_u[g] += ui * ((qi - cov) / qi); }
        }
    }
    if (g >= 0) { o->qty_end[g] = qty; o->cost_end[g] = cost; o->open_t[g] = qty > 0 ? trip_open : -1; }
    return n;
}
