/* BorderGlow
 *
 * Framework-free adapter of the React Bits BorderGlow component. Polybot's
 * dashboard is served as plain HTML, so this keeps the component's public
 * options and pointer geometry without adding a React build pipeline.
 */

'use strict';

(() => {
  const BOX_SELECTOR = [
    '.card',
    '.controlbar',
    '.note',
    '.warnbox',
    '.pcard',
    '.vol-row',
    '.regime',
    '.metricrow',
    '.wl-summary',
    '.chainlink-box',
    '.ptile',
    '.quality',
    '.toast',
  ].join(', ');

  const DEFAULTS = Object.freeze({
    edgeSensitivity: 68,
    glowColor: '43 84 84',
    backgroundColor:
      'linear-gradient(145deg, rgba(38, 34, 22, .97), rgba(26, 23, 14, .99))',
    borderRadius: 10,
    glowRadius: 20,
    glowIntensity: 0.72,
    coneSpread: 18,
    animated: false,
    colors: ['#fae8b4', '#38d39f', '#f4c76b'],
    fillOpacity: 0.12,
  });

  const GRADIENT_POSITIONS = [
    '80% 55%', '69% 34%', '8% 6%', '41% 38%',
    '86% 85%', '82% 18%', '51% 4%',
  ];
  const GRADIENT_KEYS = [
    '--gradient-one', '--gradient-two', '--gradient-three', '--gradient-four',
    '--gradient-five', '--gradient-six', '--gradient-seven',
  ];
  const COLOR_MAP = [0, 1, 2, 0, 1, 2, 1];

  function parseHSL(value) {
    const match = String(value).match(/([\d.]+)\s*([\d.]+)%?\s*([\d.]+)%?/);
    if (!match) return { h: 40, s: 80, l: 80 };
    return { h: Number(match[1]), s: Number(match[2]), l: Number(match[3]) };
  }

  function setGlowVars(card, glowColor, intensity) {
    const { h, s, l } = parseHSL(glowColor);
    const opacities = [100, 60, 50, 40, 30, 20, 10];
    const keys = ['', '-60', '-50', '-40', '-30', '-20', '-10'];

    opacities.forEach((opacity, index) => {
      const alpha = Math.min(opacity * intensity, 100);
      card.style.setProperty(
        `--glow-color${keys[index]}`,
        `hsl(${h}deg ${s}% ${l}% / ${alpha}%)`,
      );
    });
  }

  function setGradientVars(card, colors) {
    GRADIENT_KEYS.forEach((key, index) => {
      const colorIndex = Math.min(COLOR_MAP[index], colors.length - 1);
      card.style.setProperty(
        key,
        `radial-gradient(at ${GRADIENT_POSITIONS[index]}, ` +
          `${colors[colorIndex]} 0px, transparent 50%)`,
      );
    });
    card.style.setProperty('--gradient-base', `linear-gradient(${colors[0]} 0 100%)`);
  }

  function centerOf(card) {
    const { width, height } = card.getBoundingClientRect();
    return [width / 2, height / 2];
  }

  function edgeProximity(card, x, y) {
    const [cx, cy] = centerOf(card);
    const dx = x - cx;
    const dy = y - cy;
    const kx = dx === 0 ? Infinity : cx / Math.abs(dx);
    const ky = dy === 0 ? Infinity : cy / Math.abs(dy);
    return Math.min(Math.max(1 / Math.min(kx, ky), 0), 1);
  }

  function cursorAngle(card, x, y) {
    const [cx, cy] = centerOf(card);
    const dx = x - cx;
    const dy = y - cy;
    if (dx === 0 && dy === 0) return 0;
    const degrees = Math.atan2(dy, dx) * (180 / Math.PI) + 90;
    return degrees < 0 ? degrees + 360 : degrees;
  }

  function animateValue({
    start = 0,
    end = 100,
    duration = 1000,
    delay = 0,
    ease = (x) => 1 - Math.pow(1 - x, 3),
    onUpdate,
    onEnd,
  }) {
    const begin = performance.now() + delay;
    const tick = () => {
      const elapsed = performance.now() - begin;
      const progress = Math.max(0, Math.min(elapsed / duration, 1));
      onUpdate(start + (end - start) * ease(progress));
      if (progress < 1) requestAnimationFrame(tick);
      else if (onEnd) onEnd();
    };
    window.setTimeout(() => requestAnimationFrame(tick), delay);
  }

  function playSweep(card) {
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;

    const angleStart = 110;
    const angleEnd = 465;
    const setAngle = (value) => {
      const angle = (angleEnd - angleStart) * (value / 100) + angleStart;
      card.style.setProperty('--cursor-angle', `${angle}deg`);
    };

    card.classList.add('sweep-active');
    card.style.setProperty('--cursor-angle', `${angleStart}deg`);
    animateValue({
      duration: 500,
      onUpdate: (value) => card.style.setProperty('--edge-proximity', value),
    });
    animateValue({
      duration: 1500,
      end: 50,
      ease: (x) => x * x * x,
      onUpdate: setAngle,
    });
    animateValue({
      delay: 1500,
      duration: 2250,
      start: 50,
      end: 100,
      onUpdate: setAngle,
    });
    animateValue({
      delay: 2500,
      duration: 1500,
      start: 100,
      end: 0,
      ease: (x) => x * x * x,
      onUpdate: (value) => card.style.setProperty('--edge-proximity', value),
      onEnd: () => card.classList.remove('sweep-active'),
    });
  }

  function mount(card, options = {}) {
    if (!card) return card;

    const hasStructure =
      card.querySelector(':scope > .edge-light') &&
      card.querySelector(':scope > .border-glow-inner');
    if (card.dataset.borderGlowMounted === 'true' && hasStructure) return card;
    if (card.dataset.borderGlowMounted === 'true') {
      delete card.dataset.borderGlowMounted;
    }

    const config = { ...DEFAULTS, ...options };
    const computed = window.getComputedStyle(card);
    const computedBackground = computed.backgroundImage !== 'none'
      ? computed.backgroundImage
      : computed.backgroundColor;
    const computedRadius = Number.parseFloat(computed.borderTopLeftRadius);
    const backgroundColor = options.backgroundColor ??
      card.dataset.glowBackground ??
      computedBackground ??
      DEFAULTS.backgroundColor;
    const borderRadius = options.borderRadius ??
      (Number.isFinite(computedRadius) ? computedRadius : DEFAULTS.borderRadius);
    const colors = Array.isArray(config.colors) && config.colors.length
      ? config.colors.slice(0, 3)
      : DEFAULTS.colors;

    card.dataset.borderGlowMounted = 'true';
    card.classList.add('border-glow-card');
    card.style.setProperty('--card-bg', backgroundColor);
    card.style.setProperty('--edge-sensitivity', config.edgeSensitivity);
    card.style.setProperty('--border-radius', `${borderRadius}px`);
    card.style.setProperty('--glow-padding', `${config.glowRadius}px`);
    card.style.setProperty('--cone-spread', config.coneSpread);
    card.style.setProperty('--fill-opacity', config.fillOpacity);
    setGlowVars(card, config.glowColor, config.glowIntensity);
    setGradientVars(card, colors);

    const inner = document.createElement('div');
    inner.className = 'border-glow-inner';
    while (card.firstChild) inner.appendChild(card.firstChild);

    const edgeLight = document.createElement('span');
    edgeLight.className = 'edge-light';
    edgeLight.setAttribute('aria-hidden', 'true');
    card.append(edgeLight, inner);

    card.addEventListener('pointermove', (event) => {
      const rect = card.getBoundingClientRect();
      const x = event.clientX - rect.left;
      const y = event.clientY - rect.top;
      const edge = edgeProximity(card, x, y);
      const angle = cursorAngle(card, x, y);
      card.style.setProperty('--edge-proximity', (edge * 100).toFixed(3));
      card.style.setProperty('--cursor-angle', `${angle.toFixed(3)}deg`);
    }, { passive: true });

    if (config.animated) playSweep(card);
    return card;
  }

  function mountAll(selector = BOX_SELECTOR, options = {}) {
    return [...document.querySelectorAll(selector)].map((card) => mount(card, options));
  }

  function watch(selector = BOX_SELECTOR) {
    const observer = new MutationObserver((records) => {
      const candidates = new Set();
      records.forEach((record) => {
        if (record.target instanceof Element && record.target.matches(selector)) {
          candidates.add(record.target);
        }
        record.addedNodes.forEach((node) => {
          if (!(node instanceof Element)) return;
          if (node.matches(selector)) candidates.add(node);
          node.querySelectorAll(selector).forEach((match) => candidates.add(match));
        });
      });
      candidates.forEach((candidate) => mount(candidate));
    });
    observer.observe(document.body, { childList: true, subtree: true });
    return observer;
  }

  window.BorderGlow = Object.freeze({
    mount,
    mountAll,
    watch,
    selector: BOX_SELECTOR,
    defaults: DEFAULTS,
  });
  document.addEventListener('DOMContentLoaded', () => {
    mountAll();
    watch();
  });
})();
