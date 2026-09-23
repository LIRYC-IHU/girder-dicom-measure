import { describe, it, expect } from 'vitest';
import { createWheelStepper, PIXELS_PER_SLICE } from './wheelScroll';

describe('createWheelStepper', () => {
  it('avance d’une coupe par cran de molette', () => {
    const step = createWheelStepper();
    expect(step(100)).toBe(1);
    expect(step(100)).toBe(1);
    expect(step(-120)).toBe(-1);
  });

  it('n’avance pas à chaque micro-événement de trackpad', () => {
    const step = createWheelStepper();
    const steps = Array.from({ length: 8 }, () => step(4)); // 32 px cumulés < seuil
    expect(steps).toEqual([0, 0, 0, 0, 0, 0, 0, 0]);
  });

  it('avance une fois le seuil franchi, et garde le reste', () => {
    const step = createWheelStepper();
    let total = 0;
    for (let i = 0; i < 20; i++) total += step(5); // 100 px cumulés
    expect(total).toBe(Math.trunc(100 / PIXELS_PER_SLICE));
    // Le reste (20 px) est conservé : 20 px de plus suffisent à la coupe suivante.
    expect(step(20)).toBe(1);
  });

  it('repart de zéro quand le geste change de sens', () => {
    const step = createWheelStepper();
    expect(step(30)).toBe(0);
    // Sans remise à zéro, le cumul retomberait à 0 et le geste inverse serait perdu.
    expect(step(-30)).toBe(0);
    expect(step(-10)).toBe(-1); // -40 px cumulés dans le nouveau sens
  });

  it('convertit les modes ligne et page', () => {
    const step = createWheelStepper();
    expect(step(3, 1)).toBe(1); // 3 lignes → 48 px → cran
    expect(step(1, 2)).toBe(1); // 1 page
  });
});
