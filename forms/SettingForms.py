from flask_wtf import FlaskForm
from wtforms import StringField, SubmitField, IntegerField
from wtforms.validators import DataRequired
from wtforms.widgets import PasswordInput

# Определение класса формы
class SettingsForm(FlaskForm):
    host = StringField('Host', validators=[DataRequired()])
    port = IntegerField('Port', validators=[DataRequired()], default=1883)
    login = StringField('Login', validators=[DataRequired()])
    password = StringField('Password', validators=[DataRequired()], widget=PasswordInput(hide_value=False))
    topic = StringField('Topic', validators=[DataRequired()])
    submit = SubmitField('Submit')