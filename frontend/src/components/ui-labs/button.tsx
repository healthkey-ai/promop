import * as React from "react";
import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@/lib/utils";

const buttonVariants = cva(
  "inline-flex items-center justify-center whitespace-nowrap rounded-md text-sm font-semibold transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-healthkey-brand-700 focus-visible:ring-offset-2 disabled:pointer-events-none disabled:opacity-50 disabled:cursor-not-allowed",
  {
    variants: {
      variant: {
        primary:
          "bg-healthkey-brand-700 text-healthkey-text-white hover:bg-healthkey-brand-alt active:bg-healthkey-brand-700/90",
        secondary:
          "bg-healthkey-bg-primary border border-healthkey-border-secondary text-healthkey-text-primary hover:bg-healthkey-bg-secondary",
        ghost: "text-healthkey-text-primary hover:bg-healthkey-bg-secondary",
        destructive: "bg-error-700 text-white hover:bg-error-700/90",
        link: "text-healthkey-link-primary underline-offset-4 hover:text-healthkey-link-hover hover:underline",
        outline:
          "border border-healthkey-border-secondary bg-healthkey-bg-primary text-healthkey-text-primary hover:bg-healthkey-bg-secondary",
      },
      size: {
        sm: "h-9 px-3",
        md: "h-11 px-5",
        lg: "h-12 px-6 text-body-lg",
        icon: "h-10 w-10",
      },
    },
    defaultVariants: {
      variant: "primary",
      size: "md",
    },
  },
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean;
}

const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild = false, ...props }, ref) => {
    const Comp = asChild ? Slot : "button";
    return (
      <Comp className={cn(buttonVariants({ variant, size, className }))} ref={ref} {...props} />
    );
  },
);
Button.displayName = "Button";

export { Button };
