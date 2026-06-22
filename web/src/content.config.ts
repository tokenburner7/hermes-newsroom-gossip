import { defineCollection, z } from 'astro:content';
import { glob } from 'astro/loaders';

// One published article == one Markdown file in src/content/articles/.
// The Zod schema mirrors the ArticleEnvelope fields emitted by the publish
// pipeline (src/newsroom/pipeline/publish.py). Keep the two in sync.
const articles = defineCollection({
  loader: glob({ pattern: '**/*.{md,mdx}', base: './src/content/articles' }),
  schema: z.object({
    // research_synthesis, regulatory_signal, ...
    type: z.string(),
    headline: z.string(),
    dek: z.string(),
    published_at: z.coerce.date(),
    // The Markdown article body (also rendered from the file body via <Content/>).
    body_md: z.string(),
    // "<finding> -> <implication>" pairs — the angle on each fact.
    implications: z.array(z.string()),
    sources: z.array(
      z.object({
        url: z.string(),
        title: z.string(),
      }),
    ),
    claim_evidence: z.array(z.object({
      claim_id: z.number(),
      claim_text: z.string(),
      supporting_span: z.string(),
      span_sha256: z.string(),
      source_url: z.string(),
      source_title: z.string(),
    })).optional().default([]),
    // auto_gated | human_reviewed | queued
    review_path: z.string(),
    // The disclosure text shown in the badge (O-C1, honest disclosure).
    label: z.string(),
    tags: z.array(z.string()),
  }),
});

export const collections = { articles };
