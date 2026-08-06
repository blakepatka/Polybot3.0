'use strict';

/**
 * Dependency-free integration of React Bits' Iridescence shader for Polybot's
 * existing vanilla frontend. Options mirror the original React component.
 */
class Iridescence {
  constructor(container, options = {}) {
    this.container = container;
    this.options = {
      color: [1, 1, 1],
      speed: 1,
      amplitude: 0.1,
      mouseReact: true,
      ...options,
    };
    this.mouse = [0.5, 0.5];
    this.raf = null;
    this.resizeTimer = null;
    this.startedAt = performance.now();

    this.canvas = document.createElement('canvas');
    this.container.appendChild(this.canvas);
    this.gl = this.canvas.getContext('webgl', {
      alpha: false,
      antialias: false,
      powerPreference: 'low-power',
    });
    if (!this.gl) {
      this.canvas.remove();
      return;
    }

    this.onResize = this.onResize.bind(this);
    this.onMouseMove = this.onMouseMove.bind(this);
    this.onVisibilityChange = this.onVisibilityChange.bind(this);
    this.render = this.render.bind(this);

    try {
      this.initProgram();
    } catch (error) {
      console.warn('Iridescence background could not start.', error);
      this.destroy();
      return;
    }

    window.addEventListener('resize', this.onResize, { passive: true });
    document.addEventListener('visibilitychange', this.onVisibilityChange);
    if (this.options.mouseReact) {
      this.container.addEventListener('mousemove', this.onMouseMove, { passive: true });
    }

    this.resize();
    this.raf = requestAnimationFrame(this.render);
  }

  compileShader(type, source) {
    const shader = this.gl.createShader(type);
    this.gl.shaderSource(shader, source);
    this.gl.compileShader(shader);
    if (!this.gl.getShaderParameter(shader, this.gl.COMPILE_STATUS)) {
      const message = this.gl.getShaderInfoLog(shader);
      this.gl.deleteShader(shader);
      throw new Error(message || 'WebGL shader compilation failed');
    }
    return shader;
  }

  initProgram() {
    const vertexShader = `
      attribute vec2 uv;
      attribute vec2 position;

      varying vec2 vUv;

      void main() {
        vUv = uv;
        gl_Position = vec4(position, 0.0, 1.0);
      }
    `;

    const fragmentShader = `
      precision highp float;

      uniform float uTime;
      uniform vec3 uColor;
      uniform vec3 uResolution;
      uniform vec2 uMouse;
      uniform float uAmplitude;
      uniform float uSpeed;

      varying vec2 vUv;

      void main() {
        float mr = min(uResolution.x, uResolution.y);
        vec2 uv = (vUv.xy * 2.0 - 1.0) * uResolution.xy / mr;

        uv += (uMouse - vec2(0.5)) * uAmplitude;

        float d = -uTime * 0.5 * uSpeed;
        float a = 0.0;
        for (float i = 0.0; i < 8.0; ++i) {
          a += cos(i - d - a * uv.x);
          d += sin(uv.y * i + a);
        }
        d += uTime * 0.5 * uSpeed;
        vec3 col = vec3(
          cos(uv * vec2(d, a)) * 0.6 + 0.4,
          cos(a + d) * 0.5 + 0.5
        );
        col = cos(col * cos(vec3(d, a, 2.5)) * 0.5 + 0.5) * uColor;
        gl_FragColor = vec4(col, 1.0);
      }
    `;

    const gl = this.gl;
    const program = gl.createProgram();
    const vertex = this.compileShader(gl.VERTEX_SHADER, vertexShader);
    const fragment = this.compileShader(gl.FRAGMENT_SHADER, fragmentShader);
    gl.attachShader(program, vertex);
    gl.attachShader(program, fragment);
    gl.linkProgram(program);
    gl.deleteShader(vertex);
    gl.deleteShader(fragment);

    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      const message = gl.getProgramInfoLog(program);
      gl.deleteProgram(program);
      throw new Error(message || 'WebGL program linking failed');
    }

    this.program = program;
    this.uniforms = {
      time: gl.getUniformLocation(program, 'uTime'),
      color: gl.getUniformLocation(program, 'uColor'),
      resolution: gl.getUniformLocation(program, 'uResolution'),
      mouse: gl.getUniformLocation(program, 'uMouse'),
      amplitude: gl.getUniformLocation(program, 'uAmplitude'),
      speed: gl.getUniformLocation(program, 'uSpeed'),
    };

    // One oversized triangle covers the viewport. Position and UV are
    // interleaved to match the attributes exposed by OGL's Triangle helper.
    const vertices = new Float32Array([
      -1, -1, 0, 0,
       3, -1, 2, 0,
      -1,  3, 0, 2,
    ]);
    this.buffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, this.buffer);
    gl.bufferData(gl.ARRAY_BUFFER, vertices, gl.STATIC_DRAW);

    const stride = 4 * Float32Array.BYTES_PER_ELEMENT;
    const position = gl.getAttribLocation(program, 'position');
    const uv = gl.getAttribLocation(program, 'uv');
    gl.enableVertexAttribArray(position);
    gl.vertexAttribPointer(position, 2, gl.FLOAT, false, stride, 0);
    gl.enableVertexAttribArray(uv);
    gl.vertexAttribPointer(uv, 2, gl.FLOAT, false, stride, 2 * Float32Array.BYTES_PER_ELEMENT);

    gl.useProgram(program);
    gl.uniform3fv(this.uniforms.color, this.options.color);
    gl.uniform2fv(this.uniforms.mouse, this.mouse);
    gl.uniform1f(this.uniforms.amplitude, this.options.amplitude);
    gl.uniform1f(this.uniforms.speed, this.options.speed);
  }

  resize() {
    const dpr = Math.min(window.devicePixelRatio || 1, 1.5);
    const width = Math.max(1, Math.round(this.container.clientWidth * dpr));
    const height = Math.max(1, Math.round(this.container.clientHeight * dpr));
    if (this.canvas.width === width && this.canvas.height === height) return;
    this.canvas.width = width;
    this.canvas.height = height;
    this.gl.viewport(0, 0, width, height);
    this.gl.useProgram(this.program);
    this.gl.uniform3f(this.uniforms.resolution, width, height, width / height);
  }

  onResize() {
    clearTimeout(this.resizeTimer);
    this.resizeTimer = setTimeout(() => this.resize(), 100);
  }

  onMouseMove(event) {
    const rect = this.container.getBoundingClientRect();
    this.mouse[0] = (event.clientX - rect.left) / rect.width;
    this.mouse[1] = 1 - (event.clientY - rect.top) / rect.height;
    this.gl.useProgram(this.program);
    this.gl.uniform2fv(this.uniforms.mouse, this.mouse);
  }

  onVisibilityChange() {
    if (document.hidden) {
      cancelAnimationFrame(this.raf);
      this.raf = null;
    } else if (!this.raf) {
      this.startedAt = performance.now();
      this.raf = requestAnimationFrame(this.render);
    }
  }

  render(now) {
    const gl = this.gl;
    gl.useProgram(this.program);
    gl.uniform1f(this.uniforms.time, (now - this.startedAt) * 0.001);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
    this.raf = requestAnimationFrame(this.render);
  }

  destroy() {
    if (this.raf) cancelAnimationFrame(this.raf);
    clearTimeout(this.resizeTimer);
    window.removeEventListener('resize', this.onResize);
    document.removeEventListener('visibilitychange', this.onVisibilityChange);
    if (this.options.mouseReact) {
      this.container.removeEventListener('mousemove', this.onMouseMove);
    }
    if (this.gl) {
      if (this.buffer) this.gl.deleteBuffer(this.buffer);
      if (this.program) this.gl.deleteProgram(this.program);
      this.gl.getExtension('WEBGL_lose_context')?.loseContext();
    }
    this.canvas?.remove();
  }
}

const iridescenceHost = document.getElementById('iridescence');
if (iridescenceHost && !window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
  window.polybotIridescence = new Iridescence(iridescenceHost, {
    color: [0.6313725490196078, 0.611764705882353, 0.5215686274509804],
    mouseReact: false,
    amplitude: 0.1,
    speed: 0.3,
  });
}
