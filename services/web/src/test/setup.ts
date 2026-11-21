import '@testing-library/jest-dom/vitest';
import type { ReactNode } from 'react';
import { afterEach, vi } from 'vitest';
import { cleanup } from '@testing-library/react';

vi.mock('@vis.gl/react-maplibre', async () => {
  const React = await import('react');
  type MockMapProps = {
    children?: ReactNode;
    latitude?: number;
    longitude?: number;
    zoom?: number;
    className?: string;
  };
  type MockMarkerProps = MockMapProps;

  const MockMap = ({ children, latitude, longitude, zoom }: MockMapProps) => React.createElement(
    'div',
    {
      role: 'region',
      'aria-label': 'Interactive geographic map of Europe',
      tabIndex: 0,
      'data-testid': 'maplibre-map',
      'data-latitude': latitude,
      'data-longitude': longitude,
      'data-map-zoom': zoom,
    },
    children,
  );
  const MockMarker = ({ children, latitude, longitude, className }: MockMarkerProps) => React.createElement(
    'div',
    { className, 'data-marker-latitude': latitude, 'data-marker-longitude': longitude },
    children,
  );

  return { Map: MockMap, default: MockMap, Marker: MockMarker };
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

Object.defineProperty(window, 'scrollTo', { value: vi.fn(), writable: true });
Object.defineProperty(window, 'matchMedia', {
  value: vi.fn().mockImplementation((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })),
  writable: true,
});
