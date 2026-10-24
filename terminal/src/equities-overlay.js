/** Real-price line on an existing TickChart. No candle or marker paint of its own. */

export function createReferenceOverlay(chart) {
  const overlay = {
    points: [],
    setPoints(points) {
      this.points = Array.isArray(points) ? points : [];
      chart.touchReferenceOverlay?.();
    },
    range(t0, t1) {
      let min = Infinity, max = -Infinity, n = 0;
      for (const point of this.points) {
        if (point.time < t0) continue;
        if (point.time > t1) break;
        if (!(point.close > 0)) continue;
        min = Math.min(min, point.close);
        max = Math.max(max, point.close);
        n++;
      }
      return n ? {min, max} : null;
    },
    paint(ctx, host) {
      const view = host.view;
      const data = host.visible;
      if (!view || !data?.length || !this.points.length) return;
      const indexByTime = new Map();
      for (let i = 0; i < data.length; i++) indexByTime.set(data[i].time, i);
      ctx.save();
      ctx.beginPath();
      ctx.rect(0, host.priceTop, view.width, host.priceHeight);
      ctx.clip();
      ctx.lineJoin = 'round';
      ctx.lineCap = 'round';
      ctx.lineWidth = 2.25;
      ctx.strokeStyle = '#1f4b99';
      let open = false;
      for (const point of this.points) {
        const index = indexByTime.get(point.time);
        if (index == null || !(point.close > 0)) continue;
        const x = view.x(index);
        const y = view.y(point.close);
        if (!open || point.gap) {
          if (open) ctx.stroke();
          ctx.beginPath();
          ctx.moveTo(x, y);
          open = true;
        } else ctx.lineTo(x, y);
      }
      if (open) ctx.stroke();
      ctx.restore();
    },
  };
  chart.setReferenceOverlay(overlay);
  return overlay;
}
