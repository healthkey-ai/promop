import { useState } from "react";
import { Button } from "@/components/ui-labs/button";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui-labs/dialog";
import type { LabResultValue } from "@/federation/types";

interface EditMeasurementDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  measurement: LabResultValue | null;
  onSave: (data: {
    measurementId: number;
    value?: number | null;
    value_string?: string | null;
    measured_at?: string;
    range_low?: number | null;
    range_high?: number | null;
  }) => void;
  isPending: boolean;
}

export function EditMeasurementDialog({
  open,
  onOpenChange,
  measurement,
  onSave,
  isPending,
}: EditMeasurementDialogProps) {
  const [value, setValue] = useState("");
  const [valueString, setValueString] = useState("");
  const [measuredAt, setMeasuredAt] = useState("");
  const [rangeLow, setRangeLow] = useState("");
  const [rangeHigh, setRangeHigh] = useState("");

  const isQualitative = measurement?.value == null && measurement?.value_string != null;

  const handleOpen = (next: boolean) => {
    if (next && measurement) {
      setValue(measurement.value != null ? String(Number(measurement.value)) : "");
      setValueString(measurement.value_string ?? "");
      setMeasuredAt(measurement.measured_at ?? "");
      setRangeLow(measurement.range_low != null ? String(Number(measurement.range_low)) : "");
      setRangeHigh(measurement.range_high != null ? String(Number(measurement.range_high)) : "");
    }
    onOpenChange(next);
  };

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!measurement) return;

    onSave({
      measurementId: measurement.measurement_id,
      value: value ? Number(value) : null,
      value_string: valueString || null,
      measured_at: measuredAt || undefined,
      range_low: rangeLow ? Number(rangeLow) : null,
      range_high: rangeHigh ? Number(rangeHigh) : null,
    });
  };

  return (
    <Dialog open={open} onOpenChange={handleOpen}>
      <DialogContent className="max-w-sm">
        <DialogHeader>
          <DialogTitle>Edit measurement</DialogTitle>
        </DialogHeader>
        <form onSubmit={handleSubmit} className="space-y-3">
          {isQualitative ? (
            <label className="block">
              <span className="text-sm font-medium text-foreground">Value</span>
              <input
                type="text"
                value={valueString}
                onChange={(e) => setValueString(e.target.value)}
                className="mt-1 block w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
              />
            </label>
          ) : (
            <label className="block">
              <span className="text-sm font-medium text-foreground">Value</span>
              <input
                type="number"
                step="any"
                value={value}
                onChange={(e) => setValue(e.target.value)}
                className="mt-1 block w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
              />
            </label>
          )}
          <label className="block">
            <span className="text-sm font-medium text-foreground">Measured at</span>
            <input
              type="date"
              value={measuredAt}
              onChange={(e) => setMeasuredAt(e.target.value)}
              className="mt-1 block w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
            />
          </label>
          <div className="grid grid-cols-2 gap-3">
            <label className="block">
              <span className="text-sm font-medium text-foreground">Range low</span>
              <input
                type="number"
                step="any"
                value={rangeLow}
                onChange={(e) => setRangeLow(e.target.value)}
                className="mt-1 block w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
              />
            </label>
            <label className="block">
              <span className="text-sm font-medium text-foreground">Range high</span>
              <input
                type="number"
                step="any"
                value={rangeHigh}
                onChange={(e) => setRangeHigh(e.target.value)}
                className="mt-1 block w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
              />
            </label>
          </div>
          <DialogFooter className="gap-2 sm:gap-0">
            <Button variant="outline" type="button" onClick={() => onOpenChange(false)} disabled={isPending}>
              Cancel
            </Button>
            <Button type="submit" disabled={isPending}>
              {isPending ? "Saving…" : "Save"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
