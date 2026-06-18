import { createRoot } from 'react-dom/client';
import App from './App';
import './styles.css';

// Pas de StrictMode : le double-montage des effets en dev détruit/recrée le
// RenderingEngine Cornerstone (IDs globaux) pendant le setStack async → canvas 0×0.
createRoot(document.getElementById('root')!).render(<App />);
