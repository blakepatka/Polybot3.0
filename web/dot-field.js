'use strict';

/**
 * Interactive dotted overlay layered above the Iridescence shader.
 * Dots gently bulge away from the pointer and settle back into their grid.
 */
class DotField {
  constructor(container, options = {}) {
    this.container = container;
    this.options = {
      radius: 1.15,
      spacing: 17,
      cursorRadius: 360,
      strength: 42,
      ...options,
    };
    this.pointer = { x: -9999, y: -9999, active: false };
    this.dots = [];
    this.raf = null;
    this.resizeTimer = null;
    this.dpr = Math.min(window.devicePixelRatio || 1, 1.5);

    this.canvas = document.createElement('canvas');
    this.container.appendChild(this.canvas);
    this.ctx = this.canvas.getContext('2d', { alpha: true });
    if (!this.ctx) return;

    this.onPointerMove = this.onPointerMove.bind(this);
    this.onPointerLeave = this.onPointerLeave.bind(this);
    this.onResize = this.onResize.bind(this);
    this.render = this.render.bind(this);

    window.addEventListener('pointermove', this.onPointerMove, { passive: true });
    document.documentElement.addEventListener('pointerleave', this.onPointerLeave);
    window.addEventListener('resize', this.onResize, { passive: true });

    this.resize();
    this.raf = requestAnimationFrame(this.render);
  }

  resize() {
    this.width = this.container.clientWidth;
    this.height = this.container.clientHeight;
    this.canvas.width = Math.round(this.width * this.dpr);
    this.canvas.height = Math.round(this.height * this.dpr);
    this.ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);

    const step = this.options.spacing;
    const cols = Math.ceil(this.width / step);
    const rows = Math.ceil(this.height / step);
    const offsetX = (this.width - (cols - 1) * step) / 2;
    const offsetY = (this.height - (rows - 1) * step) / 2;
    this.dots = [];

    for (let row = 0; row < rows; row += 1) {
      for (let col = 0; col < cols; col += 1) {
        const x = offsetX + col * step;
        const y = offsetY + row * step;
        this.dots.push({ x, y, drawX: x, drawY: y });
      }
    }
  }

  onResize() {
    clearTimeout(this.resizeTimer);
    this.resizeTimer = setTimeout(() => this.resize(), 100);
  }

  onPointerMove(event) {
    this.pointer.x = event.clientX;
    this.pointer.y = event.clientY;
    this.pointer.active = true;
  }

  onPointerLeave() {
    this.pointer.active = false;
  }

  render() {
    const ctx = this.ctx;
    const { cursorRadius, strength, radius } = this.options;
    ctx.clearRect(0, 0, this.width, this.height);

    const gradient = ctx.createLinearGradient(0, 0, this.width, this.height);
    gradient.addColorStop(0, 'rgba(250, 232, 180, 0.23)');
    gradient.addColorStop(1, 'rgba(203, 189, 147, 0.12)');
    ctx.fillStyle = gradient;
    ctx.beginPath();

    for (const dot of this.dots) {
      let targetX = dot.x;
      let targetY = dot.y;

      if (this.pointer.active) {
        const dx = this.pointer.x - dot.x;
        const dy = this.pointer.y - dot.y;
        const distance = Math.hypot(dx, dy);
        if (distance < cursorRadius && distance > 0) {
          const falloff = 1 - distance / cursorRadius;
          const push = falloff * falloff * strength;
          targetX -= (dx / distance) * push;
          targetY -= (dy / distance) * push;
        }
      }

      dot.drawX += (targetX - dot.drawX) * 0.14;
      dot.drawY += (targetY - dot.drawY) * 0.14;
      ctx.moveTo(dot.drawX + radius, dot.drawY);
      ctx.arc(dot.drawX, dot.drawY, radius, 0, Math.PI * 2);
    }

    ctx.fill();
    this.raf = requestAnimationFrame(this.render);
  }

  destroy() {
    if (this.raf) cancelAnimationFrame(this.raf);
    clearTimeout(this.resizeTimer);
    window.removeEventListener('pointermove', this.onPointerMove);
    document.documentElement.removeEventListener('pointerleave', this.onPointerLeave);
    window.removeEventListener('resize', this.onResize);
    this.canvas.remove();
  }
}

const dotFieldHost = document.getElementById('dot-field');
if (dotFieldHost && !window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
  window.polybotDotField = new DotField(dotFieldHost);
}
