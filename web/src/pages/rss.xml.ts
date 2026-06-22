import rss from '@astrojs/rss';
import { getCollection } from 'astro:content';
import type { APIContext } from 'astro';

// Brand URL fallback when the build has no configured `site`.
const BRAND_URL = 'https://aixcrypto.news';

export async function GET(context: APIContext) {
  const articles = (await getCollection('articles')).sort(
    (a, b) => b.data.published_at.valueOf() - a.data.published_at.valueOf(),
  );

  return rss({
    title: 'The Gossip',
    description:
      'Sourced celebrity news and entertainment gossip — every claim provenance-locked to its source.',
    site: context.site ?? BRAND_URL,
    items: articles.map((article) => ({
      title: article.data.headline,
      pubDate: article.data.published_at,
      description: article.data.dek,
      link: `/articles/${article.id}/`,
    })),
  });
}
