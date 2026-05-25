import React from 'react';
import { VocabularyTooltip } from './VocabularyTooltip';
import { VocabSource } from '@/hooks/useVocabulary';

interface FormFieldProps {
  label: string;
  children: React.ReactNode;
  className?: string;
  vocabSource?: VocabSource | null;
}

export const FormField: React.FC<FormFieldProps> = ({ label, children, className = '', vocabSource }) => {
  return (
    <div className={`flex items-start gap-4 ${className}`}>
      <label className="text-sm font-medium text-gray-700 w-48 pt-2 text-left flex items-center">
        {label}
        {vocabSource && <VocabularyTooltip name={vocabSource.name} url={vocabSource.url} />}
      </label>
      <div className="flex-1">
        {children}
      </div>
    </div>
  );
};
