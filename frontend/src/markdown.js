import { createElement } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";

function MarkdownLink({ node: _node, href = "", children, ...props }) {
  const external = /^https?:\/\//i.test(href);
  return createElement(
    "a",
    {
      ...props,
      ...(href ? { href } : {}),
      ...(external ? { target: "_blank", rel: "noreferrer noopener" } : {}),
    },
    children,
  );
}

const MARKDOWN_COMPONENTS = {
  a: MarkdownLink,
};

export default function RichText({ content }) {
  return createElement(
    "div",
    { className: "rich-text" },
    createElement(
      Markdown,
      {
        remarkPlugins: [remarkGfm],
        components: MARKDOWN_COMPONENTS,
      },
      String(content || ""),
    ),
  );
}
