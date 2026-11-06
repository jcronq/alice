import { useMemo } from "react";
import { marked } from "marked";

export interface SectionProps {
  // Human-readable heading for the section. Rendered in a header
  // above the markdown body (lets us show the heading even when the
  // server's markdown doesn't include it).
  label: string;
  markdown: string;
}

marked.setOptions({
  gfm: true,
  breaks: false,
});

export function Section({ label, markdown }: SectionProps): JSX.Element {
  const html = useMemo(() => {
    if (!markdown) return "";
    // ``marked.parse`` returns a Promise when ``async: true`` — we
    // keep it synchronous (default) and take the string result.
    return marked.parse(markdown) as string;
  }, [markdown]);

  const empty = !markdown.trim();
  return (
    <section className={`rendered-section${empty ? " empty" : ""}`}>
      <h2>{label}</h2>
      {empty ? (
        <p>
          <em>(no content yet — waiting on the first poll)</em>
        </p>
      ) : (
        <div dangerouslySetInnerHTML={{ __html: html }} />
      )}
    </section>
  );
}
