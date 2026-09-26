/**
 * WebSocket client for `GET /api/stream` (docs/CONTRACT.md §6.2).
 *
 * - URL derived from `location` (falls back to the API base when there is no location).
 * - Auto-reconnect with exponential backoff and jitter; heartbeat watchdog.
 * - `status` is reactive-friendly: `subscribeStatus` callbacks plus a `status` getter.
 * - `subscribe(patterns, severities)` sends the §6.2 subscribe frame (empty = all).
 * - `ping` every 25 s (server answers `pong`; also keeps intermediaries from idling us out).
 */

import type { ClientFrame, PingFrame, ServerFrame, Severity, SubscribeFrame } from './types';

export type StreamStatus = 'connecting' | 'live' | 'reconnecting' | 'offline';

export interface StreamStats {
	/** Frames received since the page opened. */
	frames: number;
	/** `tick` frames received. */
	ticks: number;
	/** Latest `summary.tick` reported by the server (tick counter). */
	lastTick: number | null;
	/** Latest `tick` day. */
	day: string | null;
	/** Reconnect attempts since the last successful open. */
	reconnects: number;
	/** Timestamp (ms) of the last frame of any kind. */
	lastFrameAt: number | null;
}

export interface StreamHandlers {
	onFrame?: (frame: ServerFrame) => void;
	onStatus?: (status: StreamStatus) => void;
	onStats?: (stats: StreamStats) => void;
}

const PING_INTERVAL_MS = 25_000;
const WATCHDOG_MS = 45_000;
const BACKOFF_BASE_MS = 500;
const BACKOFF_MAX_MS = 15_000;

/** `${wss|ws}://${location.host}/api/stream`, per §7. */
export function streamUrl(): string {
	if (typeof location !== 'undefined' && location.host) {
		const proto = location.protocol === 'https:' ? 'wss' : 'ws';
		return `${proto}://${location.host}/api/stream`;
	}
	const base = (import.meta.env.PUBLIC_API_BASE as string | undefined) ?? '/api';
	const absolute = new URL(base, 'http://localhost');
	const proto = absolute.protocol === 'https:' ? 'wss' : 'ws';
	return `${proto}://${absolute.host}${absolute.pathname.replace(/\/+$/, '')}/stream`;
}

export class StreamClient {
	private socket: WebSocket | null = null;
	private url: string;
	private handlers: StreamHandlers;
	private pingTimer: ReturnType<typeof setInterval> | null = null;
	private watchdogTimer: ReturnType<typeof setTimeout> | null = null;
	private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
	private attempt = 0;
	private closed = false;
	private patterns: string[] = [];
	private severities: Severity[] = [];

	status: StreamStatus = 'connecting';
	stats: StreamStats = { frames: 0, ticks: 0, lastTick: null, day: null, reconnects: 0, lastFrameAt: null };

	constructor(handlers: StreamHandlers = {}, url: string = streamUrl()) {
		this.handlers = handlers;
		this.url = url;
	}

	/** Opens the socket (idempotent). */
	connect(): void {
		if (this.closed || this.socket) return;
		this.setStatus(this.attempt === 0 ? 'connecting' : 'reconnecting');

		let socket: WebSocket;
		try {
			socket = new WebSocket(this.url);
		} catch {
			this.scheduleReconnect();
			return;
		}
		this.socket = socket;

		socket.onopen = () => {
			this.attempt = 0;
			this.setStatus('live');
			// Re-apply the connection filter: the server starts each connection unfiltered.
			if (this.patterns.length > 0 || this.severities.length > 0) {
				this.send({ type: 'subscribe', patterns: this.patterns, severities: this.severities });
			}
			this.startPing();
			this.armWatchdog();
		};

		socket.onmessage = (event: MessageEvent) => {
			this.armWatchdog();
			let frame: ServerFrame;
			try {
				frame = JSON.parse(String(event.data)) as ServerFrame;
			} catch {
				return;
			}
			this.stats.frames += 1;
			this.stats.lastFrameAt = Date.now();
			if (frame.type === 'tick') {
				this.stats.ticks += 1;
				this.stats.lastTick = frame.summary.tick;
				this.stats.day = frame.day;
			}
			this.handlers.onStats?.({ ...this.stats });
			this.handlers.onFrame?.(frame);
		};

		socket.onclose = () => {
			this.stopPing();
			this.disarmWatchdog();
			this.socket = null;
			if (!this.closed) this.scheduleReconnect();
		};

		socket.onerror = () => {
			// `onclose` follows; reconnect is handled there.
		};
	}

	/** Closes for good and releases timers. */
	close(): void {
		this.closed = true;
		this.stopPing();
		this.disarmWatchdog();
		if (this.reconnectTimer !== null) {
			clearTimeout(this.reconnectTimer);
			this.reconnectTimer = null;
		}
		const socket = this.socket;
		this.socket = null;
		if (socket) {
			socket.onopen = null;
			socket.onmessage = null;
			socket.onclose = null;
			socket.onerror = null;
			socket.close();
		}
		this.setStatus('offline');
	}

	/** Sends the §6.2 subscribe frame; an empty array means "all". */
	subscribe(patterns: string[], severities: Severity[]): void {
		this.patterns = [...patterns];
		this.severities = [...severities];
		this.send({ type: 'subscribe', patterns: this.patterns, severities: this.severities });
	}

	private send(frame: ClientFrame): void {
		if (this.socket && this.socket.readyState === WebSocket.OPEN) {
			this.socket.send(JSON.stringify(frame));
		}
	}

	private ping(): void {
		const frame: PingFrame = { type: 'ping' };
		this.send(frame);
	}

	private startPing(): void {
		this.stopPing();
		this.pingTimer = setInterval(() => this.ping(), PING_INTERVAL_MS);
	}

	private stopPing(): void {
		if (this.pingTimer !== null) {
			clearInterval(this.pingTimer);
			this.pingTimer = null;
		}
	}

	/** A silent socket is a dead socket: force a reconnect if nothing arrives in time. */
	private armWatchdog(): void {
		this.disarmWatchdog();
		this.watchdogTimer = setTimeout(() => {
			const socket = this.socket;
			this.socket = null;
			if (socket) {
				socket.onclose = null;
				socket.close();
			}
			this.stopPing();
			this.scheduleReconnect();
		}, WATCHDOG_MS);
	}

	private disarmWatchdog(): void {
		if (this.watchdogTimer !== null) {
			clearTimeout(this.watchdogTimer);
			this.watchdogTimer = null;
		}
	}

	private scheduleReconnect(): void {
		if (this.closed || this.reconnectTimer !== null) return;
		this.setStatus('reconnecting');
		this.stats.reconnects += 1;
		this.handlers.onStats?.({ ...this.stats });
		const ceiling = Math.min(BACKOFF_MAX_MS, BACKOFF_BASE_MS * 2 ** this.attempt);
		const delay = ceiling / 2 + Math.random() * (ceiling / 2);
		this.attempt = Math.min(this.attempt + 1, 10);
		this.reconnectTimer = setTimeout(() => {
			this.reconnectTimer = null;
			this.connect();
		}, delay);
	}

	private setStatus(status: StreamStatus): void {
		if (this.status === status) return;
		this.status = status;
		this.handlers.onStatus?.(status);
	}
}
