// Maps an article's review_path to its disclosure badge (O-C1: honest
// disclosure on every article). The pipeline also stores a `label` string;
// we prefer the canonical text here and fall back to the stored label.
export type ReviewPath = 'auto_gated' | 'human_reviewed' | 'queued' | string;

const TEXT: Record<string, string> = {
  auto_gated: 'AI-generated · automated quality-gated',
  human_reviewed: 'AI-assisted · human-reviewed',
  queued: 'Awaiting review',
};

export function disclosure(reviewPath: ReviewPath, label?: string) {
  const known = reviewPath in TEXT;
  return {
    text: TEXT[reviewPath] ?? label ?? 'Disclosure unavailable',
    // Class suffix used with the global .badge--<path> styles.
    cls: known ? reviewPath : 'queued',
  };
}
