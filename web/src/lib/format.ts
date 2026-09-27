/**
 * Formatting helpers for the dense data-tool aesthetic.
 * Numbers are compact and monospace-friendly; unknown/absent values render as an em dash.
 */

export const DASH = '—';

export function fmtInt(value: number | null | undefined): string {
	if (value === null || value === undefined || !Number.isFinite(value)) return DASH;
	return Math.round(value).toLocaleString('id-ID');
}

/** Compact magnitude: 1.23M / 45.6k / 789. */
export function fmtCompact(value: number | null | undefined): string {
	if (value === null || value === undefined || !Number.isFinite(value)) return DASH;
	const abs = Math.abs(value);
	if (abs >= 1e9) return `${(value / 1e9).toLocaleString('id-ID', { maximumFractionDigits: 2 })} M`;
	if (abs >= 1e6) return `${(value / 1e6).toLocaleString('id-ID', { maximumFractionDigits: 2 })} jt`;
	if (abs >= 1e4) return `${(value / 1e3).toLocaleString('id-ID', { maximumFractionDigits: 1 })} rb`;
	return value.toLocaleString('id-ID', { maximumFractionDigits: abs >= 100 ? 0 : 2 });
}

export function fmtMoney(value: number | null | undefined, digits = 2): string {
	if (value === null || value === undefined || !Number.isFinite(value)) return DASH;
	return `$${value.toLocaleString('id-ID', { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
}

export function fmtPrice(value: number | null | undefined): string {
	return fmtMoney(value, 2);
}

/** Fraction (0.312) -> "31.2%". */
export function fmtPct(value: number | null | undefined, digits = 1): string {
	if (value === null || value === undefined || !Number.isFinite(value)) return DASH;
	return `${(value * 100).toLocaleString('id-ID', { minimumFractionDigits: digits, maximumFractionDigits: digits })}%`;
}

/** Already-scaled percentage (0.02 meaning 2%) -> "+2.0%" with an explicit sign. */
export function fmtSignedPct(value: number | null | undefined, digits = 1): string {
	if (value === null || value === undefined || !Number.isFinite(value)) return DASH;
	const scaled = value * 100;
	const sign = scaled > 0 ? '+' : '';
	return `${sign}${scaled.toLocaleString('id-ID', { minimumFractionDigits: digits, maximumFractionDigits: digits })}%`;
}

export function fmtRatio(value: number | null | undefined, digits = 2): string {
	if (value === null || value === undefined || !Number.isFinite(value)) return DASH;
	return `${value.toLocaleString('id-ID', { minimumFractionDigits: digits, maximumFractionDigits: digits })}x`;
}

export function fmtDay(value: string | null | undefined): string {
	if (!value) return DASH;
	const parts = value.slice(0, 10).split('-');
	if (parts.length !== 3) return value;
	return `${parts[2]}/${parts[1]}`;
}

export function fmtDateTime(value: string | null | undefined): string {
	if (!value) return DASH;
	const date = new Date(value);
	if (Number.isNaN(date.getTime())) return value;
	return date.toLocaleString('id-ID', {
		month: 'short',
		day: '2-digit',
		hour: '2-digit',
		minute: '2-digit',
		second: '2-digit',
		hour12: false
	});
}

/** "12s ago", "4m ago" — relative to `now` (defaults to Date.now()). */
export function fmtAgo(value: string | null | undefined, now: number = Date.now()): string {
	if (!value) return DASH;
	const then = new Date(value).getTime();
	if (Number.isNaN(then)) return value;
	const seconds = Math.max(0, Math.round((now - then) / 1000));
	if (seconds < 5) return 'baru saja';
	if (seconds < 60) return `${seconds} dtk lalu`;
	const minutes = Math.floor(seconds / 60);
	if (minutes < 60) return `${minutes} mnt lalu`;
	const hours = Math.floor(minutes / 60);
	if (hours < 24) return `${hours} jam lalu`;
	return `${Math.floor(hours / 24)} hari lalu`;
}

export function fmtDuration(seconds: number | null | undefined): string {
	if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return DASH;
	if (seconds < 60) return `${Math.round(seconds)}s`;
	const minutes = Math.floor(seconds / 60);
	if (minutes < 60) return `${minutes}m ${Math.round(seconds % 60)}s`;
	return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

/** Margin banding used by the result table (red / amber / green). */
export type MarginBand = 'low' | 'mid' | 'high';

export function marginBand(margin: number | null | undefined): MarginBand {
	if (margin === null || margin === undefined || !Number.isFinite(margin)) return 'low';
	if (margin >= 0.25) return 'high';
	if (margin >= 0.15) return 'mid';
	return 'low';
}

/** Evidence values arrive as untyped JSON; render scalars, summarise the rest. */
export function fmtEvidence(value: unknown): string {
	if (value === null || value === undefined) return DASH;
	if (typeof value === 'number') {
		if (Number.isInteger(value)) return fmtInt(value);
		return value.toFixed(Math.abs(value) < 1 ? 3 : 2);
	}
	if (typeof value === 'boolean') return value ? 'ya' : 'tidak';
	if (typeof value === 'string') return value;
	if (Array.isArray(value)) return value.map((entry) => fmtEvidence(entry)).join(', ');
	return JSON.stringify(value);
}
