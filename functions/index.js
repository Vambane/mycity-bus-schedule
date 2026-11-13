/**
 * Cloudflare Pages Function: keep the landing-page query redirect.
 * The static landing page also replaces the URL in the browser when this
 * function is not deployed (direct upload of dist/ only).
 */
export async function onRequest(context) {
  const url = new URL(context.request.url);
  if (url.pathname === "/" && url.search) {
    return Response.redirect(`${url.origin}/search${url.search}`, 301);
  }
  return context.next();
}
