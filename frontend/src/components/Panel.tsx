import { useState } from "react";

// A card whose heading toggles its body. Uncontrolled: defaultOpen only sets the initial state,
// and code elsewhere may open it by setting the <details> element's `open` (jump to an error does).

interface Props extends Omit<React.HTMLAttributes<HTMLElement>, "title"> {
  title: React.ReactNode;
  badge?: React.ReactNode; // shown beside the title, e.g. a count of problems inside
  defaultOpen?: boolean;
}

export default function Panel({ title, badge, defaultOpen = true, className, children, ...rest }: Props) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <details
      {...rest}
      className={className ? `card panel ${className}` : "card panel"}
      open={open}
      onToggle={(e) => setOpen(e.currentTarget.open)}
    >
      <summary>
        <h2>{title}</h2>
        {badge}
      </summary>
      {children}
    </details>
  );
}
