let currentSlide = 0;
let slideTimer = null;

function getSlides() {
    return document.querySelectorAll(".slide");
}

function getDots() {
    return document.querySelectorAll(".dot");
}

function showSlide(index) {
    const slides = getSlides();
    const dots = getDots();

    if (!slides || slides.length === 0) {
        return;
    }

    if (index >= slides.length) {
        currentSlide = 0;
    } else if (index < 0) {
        currentSlide = slides.length - 1;
    } else {
        currentSlide = index;
    }

    slides.forEach(function(slide) {
        slide.classList.remove("active");
    });

    dots.forEach(function(dot) {
        dot.classList.remove("active");
    });

    slides[currentSlide].classList.add("active");

    if (dots[currentSlide]) {
        dots[currentSlide].classList.add("active");
    }
}

function changeSlide(step) {
    showSlide(currentSlide + step);
    restartSlideTimer();
}

function goToSlide(index) {
    showSlide(index);
    restartSlideTimer();
}

function startSlideTimer() {
    const slides = getSlides();

    if (!slides || slides.length <= 1) {
        return;
    }

    slideTimer = setInterval(function() {
        showSlide(currentSlide + 1);
    }, 5000);
}

function restartSlideTimer() {
    if (slideTimer) {
        clearInterval(slideTimer);
    }

    startSlideTimer();
}

document.addEventListener("DOMContentLoaded", function() {
    showSlide(0);
    startSlideTimer();
});