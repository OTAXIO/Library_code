"""Test-only host for the retained cross-review panel, absent from production UI."""
from app import App
from cross_review_ui import CrossReviewPanel


class CrossReviewTestApp(App):
    def build(self):
        super().build()
        self.cross_review = CrossReviewPanel(self.notebook, self)
        self.pages['cross'] = self.cross_review
        self.notebook.add(self.cross_review, text='交叉核验')
        self.notebook.select(self.cross_review)

    def configure_actions(self):
        if self.mode() != 'cross':
            return super().configure_actions()
        self.cross_review.controls()
        self.start_button.configure(state='normal' if self.cross_review.review and not self.busy else 'disabled')

    def start(self):
        if self.mode() == 'cross':
            return self.cross_review.start()
        return super().start()
