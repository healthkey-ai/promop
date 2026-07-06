"""
Django management command to query and display PatientRecord records.

This command demonstrates how to query the PatientRecord model and display
patient data in a readable format.

Usage:
    python manage.py query_patient_record
    python manage.py query_patient_record --person-id 1001
"""

from django.core.management.base import BaseCommand
from omop_core.models import PatientRecord


class Command(BaseCommand):
    help = 'Query and display PatientRecord records'

    def add_arguments(self, parser):
        parser.add_argument(
            '--person-id',
            type=int,
            help='Query specific person ID',
        )
        parser.add_argument(
            '--disease',
            type=str,
            help='Filter by disease type',
        )

    def handle(self, *args, **options):
        person_id = options.get('person_id')
        disease = options.get('disease')

        # Build query
        queryset = PatientRecord.objects.select_related('person')
        
        if person_id:
            queryset = queryset.filter(person__person_id=person_id)
        
        if disease:
            queryset = queryset.filter(disease__icontains=disease)

        # Display results
        if not queryset.exists():
            self.stdout.write(self.style.WARNING('No PatientRecord records found'))
            return

        self.stdout.write(f'Found {queryset.count()} PatientRecord record(s):\n')

        for patient_info in queryset:
            self.display_patient_info(patient_info)
            self.stdout.write('-' * 60)

    def display_patient_info(self, patient_info):
        """Display detailed patient information"""
        person = patient_info.person
        
        self.stdout.write(self.style.SUCCESS(f'Patient ID: {person.person_id}'))
        self.stdout.write(f'Age: {patient_info.patient_age}')
        self.stdout.write(f'Gender: {patient_info.gender}')
        self.stdout.write(f'Disease: {patient_info.disease}')
        self.stdout.write(f'Stage: {patient_info.stage}')
        
        # Physical measurements
        if patient_info.weight and patient_info.height:
            self.stdout.write(f'Weight: {patient_info.weight} {patient_info.weight_units}')
            self.stdout.write(f'Height: {patient_info.height} {patient_info.height_units}')
            if patient_info.bmi:
                self.stdout.write(f'BMI: {patient_info.bmi}')
        
        # Performance status
        if patient_info.ecog_performance_status is not None:
            self.stdout.write(f'ECOG Performance Status: {patient_info.ecog_performance_status}')
        if patient_info.karnofsky_performance_score:
            self.stdout.write(f'Karnofsky Score: {patient_info.karnofsky_performance_score}')
        
        # Laboratory values
        lab_values = []
        if patient_info.hemoglobin_level:
            lab_values.append(f'Hemoglobin: {patient_info.hemoglobin_level} {patient_info.hemoglobin_level_units}')
        if patient_info.platelet_count:
            lab_values.append(f'Platelets: {patient_info.platelet_count} {patient_info.platelet_count_units}')
        if patient_info.white_blood_cell_count:
            lab_values.append(f'WBC: {patient_info.white_blood_cell_count} {patient_info.white_blood_cell_count_units}')
        if patient_info.serum_creatinine_level:
            lab_values.append(f'Creatinine: {patient_info.serum_creatinine_level} {patient_info.serum_creatinine_level_units}')
        
        if lab_values:
            self.stdout.write('Laboratory Values:')
            for lab in lab_values:
                self.stdout.write(f'  {lab}')
        
        # Treatment history
        if patient_info.first_line_therapy:
            self.stdout.write(f'First Line Therapy: {patient_info.first_line_therapy}')
            if patient_info.first_line_date:
                self.stdout.write(f'First Line Date: {patient_info.first_line_date}')
            if patient_info.first_line_outcome:
                self.stdout.write(f'First Line Outcome: {patient_info.first_line_outcome}')
        
        if patient_info.therapy_lines_count:
            self.stdout.write(f'Total Therapy Lines: {patient_info.therapy_lines_count}')
        
        # Cancer-specific information
        cancer_info = []
        if patient_info.estrogen_receptor_status:
            cancer_info.append(f'ER: {patient_info.estrogen_receptor_status}')
        if patient_info.progesterone_receptor_status:
            cancer_info.append(f'PR: {patient_info.progesterone_receptor_status}')
        if patient_info.her2_status:
            cancer_info.append(f'HER2: {patient_info.her2_status}')
        
        if cancer_info:
            self.stdout.write('Cancer-Specific Information:')
            for info in cancer_info:
                self.stdout.write(f'  {info}')
        
        # Genetic mutations
        if patient_info.genetic_mutations:
            self.stdout.write(f'Genetic Mutations: {", ".join(patient_info.genetic_mutations)}')
        
        # Geographic information
        if patient_info.country or patient_info.region:
            location = []
            if patient_info.region:
                location.append(patient_info.region)
            if patient_info.country:
                location.append(patient_info.country)
            self.stdout.write(f'Location: {", ".join(location)}')
        
        # Risk factors
        risk_factors = []
        if not patient_info.no_tobacco_use_status:
            risk_factors.append('Tobacco use')
        if not patient_info.no_substance_use_status:
            risk_factors.append('Substance use')
        if not patient_info.no_active_infection_status:
            risk_factors.append('Active infection')
        
        if risk_factors:
            self.stdout.write(f'Risk Factors: {", ".join(risk_factors)}')
        else:
            self.stdout.write('No significant risk factors reported')
        
        # Language skills
        languages_display = patient_info.get_languages_display()
        if languages_display != "No languages recorded":
            self.stdout.write(f'Languages: {languages_display}')
            primary_language = patient_info.get_primary_language()
            if primary_language:
                self.stdout.write(f'Primary Language: {primary_language}')
