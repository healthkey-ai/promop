import React, { useMemo } from "react";
import { Popover, PopoverContent, PopoverTrigger } from "../../shadcn/popover";
import { Command, CommandGroup, CommandItem } from "../../shadcn/command";
import { Checkbox } from "../../shadcn/checkbox";
import { Button } from "../../shadcn/button";
import type { Option } from "../utils";
import { optionKey } from "../utils";

export default function MultiSelectControl({
  options,
  selectedValues,
  display,
  disabled,
  maxListHeight = "14rem",
  onChange,
}: {
  options: Option[];
  selectedValues: unknown[];
  display: string;
  disabled?: boolean;
  maxListHeight?: string;
  onChange: (nextValues: unknown[]) => void;
}) {
  const selectedSet = useMemo(() => {
    return new Set(selectedValues.map(optionKey));
  }, [selectedValues]);

  const toggle = (v: unknown) => {
    const next = new Set(selectedSet);
    const k = optionKey(v);
    if (next.has(k)) next.delete(k);
    else next.add(k);

    const ordered = options
      .filter((o) => next.has(optionKey(o.value)))
      .map((o) => o.value);

    onChange(ordered);
  };

  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button
          type="button"
          variant="outline"
          disabled={disabled}
          className={[
            "w-full justify-between min-w-0",
            selectedValues.length === 0 ? "text-portal-text-secondary" : "",
          ].join(" ")}
        >
          <span className="truncate">{display}</span>
          <span className="text-portal-text-secondary">▾</span>
        </Button>
      </PopoverTrigger>

      <PopoverContent className="w-[--radix-popover-trigger-width] p-0">
        <Command>
          <CommandGroup style={{ maxHeight: maxListHeight, overflow: "auto" }}>
            {options.map((o) => {
              const checked = selectedSet.has(optionKey(o.value));
              return (
                <CommandItem
                  key={optionKey(o.value)}
                  onSelect={() => toggle(o.value)}
                  className="flex items-start gap-3"
                >
                  <Checkbox checked={checked} />
                  <span className="text-sm text-portal-text-primary">{o.label}</span>
                </CommandItem>
              );
            })}
          </CommandGroup>
        </Command>
      </PopoverContent>
    </Popover>
  );
}
