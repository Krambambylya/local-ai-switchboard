import type { ButtonHTMLAttributes, ReactNode } from "react";

import controls from "../../styles/controls.module.css";

export function IconButton({
  label,
  children,
  className,
  type = "button",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { label: string; children: ReactNode }) {
  return (
    <button type={type} aria-label={label} title={label} className={`${controls.iconButton} ${className ?? ""}`} {...props}>
      {children}
    </button>
  );
}
