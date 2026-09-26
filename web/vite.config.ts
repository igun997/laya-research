import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

export default defineConfig({
	plugins: [sveltekit()],
	// PUBLIC_API_BASE is injected at build time (docker compose sets it to /api).
	envPrefix: ['VITE_', 'PUBLIC_'],
	server: {
		port: 3000,
		strictPort: false
	},
	preview: {
		port: 3000
	}
});
