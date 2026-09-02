import type { Item } from '../types'

/** Posters come straight from Plex's CDN — already hosted, stable, and mirroring
 *  a few hundred images locally would buy nothing but a cache to invalidate. */
export default function Poster({
  item, size = 'card',
}: {
  item?: Pick<Item, 'title' | 'poster_url'> | null
  size?: 'chip' | 'card' | 'row' | 'hero'
}) {
  if (!item) return <span className={`poster poster-${size} is-blank`} />
  if (!item.poster_url) {
    return (
      <span className={`poster poster-${size} is-blank`} aria-hidden="true">
        {item.title?.slice(0, 2).toUpperCase()}
      </span>
    )
  }
  return (
    <img
      className={`poster poster-${size}`}
      src={item.poster_url}
      alt=""
      loading="lazy"
      draggable={false}
    />
  )
}
